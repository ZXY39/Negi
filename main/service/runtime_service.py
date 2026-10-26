from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import httpx
from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.repair_required import RepairRequiredTool
from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool
from g3ku.config.live_runtime import get_runtime_config
from g3ku.config.loader import get_config_path
from g3ku.content import ContentNavigationService, artifact_ref_from_id
from g3ku.content.navigation import RESOURCE_URI_SCHEME
from g3ku.deployment.data_root import data_root, data_work_path, ensure_dir, resolve_data_path
from g3ku.llm_config.runtime_resolver import resolve_chat_target
from g3ku.resources.models import ResourceKind
from g3ku.resources.retired_schemas import (
    index_schemas_by_name,
    lookup_tool_schema,
    placeholder_tool_schema,
    record_tool_schema,
)
from g3ku.resources.tool_settings import (
    MemoryRuntimeSettings,
    raw_tool_settings_from_descriptor,
    validate_tool_settings,
)
from g3ku.runtime import tool_arg_stash
from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection
from g3ku.runtime.context.node_context_selection import (
    NodeContextSelectionResult,
    build_node_context_selection,
)
from g3ku.runtime.context.summarizer import layered_body_payload, score_query
from g3ku.runtime.core_tools import configured_core_tools, resolve_core_tool_targets
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.runtime.tool_context_presence import contract_presence_index
from g3ku.runtime.tool_contract_block import render_tool_contract
from g3ku.runtime.tool_error_guidance import append_parameter_error_guidance
from g3ku.runtime.tool_impulse import (
    IMPULSE_INVALID,
    IMPULSE_REJECT,
    IMPULSE_STAGED,
    impulse_receipt,
)
from g3ku.runtime.tool_visibility import (
    NODE_FIXED_BUILTIN_TOOL_NAMES,
    NODE_NEVER_CONTRACT_TOOL_NAMES,
    fixed_builtin_tool_name_set_for_actor_role,
)
from g3ku.runtime.wake_clock import iso_from_ms
from g3ku.runtime.wake_clock import now_ms as wall_now_ms
from g3ku.security import get_bootstrap_security_service
from g3ku.update_apply import WEB_LOG_FILE
from g3ku.utils.api_keys import parse_api_keys, resolve_api_key_concurrency_layout
from g3ku.utils.helpers import normalize_seed_tool_names, tools_bundle_single_source_enabled
from g3ku.web.worker_control import managed_worker_snapshot
from main.errors import TaskPausedError, describe_exception
from main.governance import (
    GovernanceStore,
    MainRuntimePolicyEngine,
    MainRuntimeResourceRegistry,
    PermissionSubject,
    list_effective_skill_ids,
    list_effective_tool_names,
)
from main.governance.exec_approvals import ExecApprovalService
from main.governance.exec_tool_policy import (
    EXEC_TOOL_ID,
    exec_tool_supports_execution_mode,
    merge_exec_execution_mode_metadata,
    resolve_exec_runtime_policy_payload,
)
from main.governance.review_policy import (
    GOVERNANCE_MODE_META_KEY,
    GOVERNANCE_MODE_UPDATED_AT_META_KEY,
    resolve_review_free_tool_ids,
    review_mode_from_enabled,
    write_review_free_tool_ids,
)
from main.governance.roles import normalize_public_allowed_roles
from main.governance.tool_context import (
    build_tool_context_fingerprint,
    build_tool_toolskill_payload,
)
from main.ids import new_command_id, new_node_id, new_task_id, new_worker_id
from main.models import (
    FrontdoorNodeMessage,
    NodeRecord,
    TaskArtifactRecord,
    TaskNodeNotification,
    TaskRecord,
    TokenUsageSummary,
    normalize_execution_policy_metadata,
    normalize_final_acceptance_metadata,
    normalize_result_payload,
)
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.mem_probe import start_mem_probe
from main.monitoring.query_service_v2 import TaskQueryServiceV2
from main.prompts import load_prompt
from main.protocol import build_envelope, now_iso
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    normalize_acceptance_handshake,
)
from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
from main.runtime.chat_backend import ChatBackend
from main.runtime.debug_recorder import RuntimeDebugRecorder
from main.runtime.execution_trace_compaction import compact_tool_step_for_summary
from main.runtime.global_scheduler import GlobalScheduler
from main.runtime.internal_tools import build_detail_level_schema
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer, quota_bucket_key
from main.runtime.model_route import build_model_route_plan
from main.runtime.node_mailbox import (
    REJECT_TARGET_LIST_UNPARSED,
    TARGET_LIST_UNPARSED_HINT,
    delivery_defers_wake,
    error_escalation_hint,
    message_fingerprint,
    normalize_targets,
    notification_is_held,
    plan_delivery,
    unresolved_target_reason,
)
from main.runtime.node_message_kind import (
    KIND_ACCEPTANCE_VERDICT,
    KIND_CHILD_ABNORMAL_PAUSE,
    KIND_CHILD_FINAL,
    KIND_WAKE_TIMER,
    ORIGIN_FRONTDOOR,
    ORIGIN_NODE,
    ORIGIN_USER,
    PRIORITY_HIGH,
    RELATION_CHILD,
    RELATION_PARENT,
    derive_message_kind,
    derive_priority,
)
from main.runtime.node_prompt_contract import (
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
    inject_node_dynamic_contract_message,
)
from main.runtime.node_relationships import (
    is_descendant as node_is_descendant,
)
from main.runtime.node_relationships import (
    message_relation as node_message_relation,
)
from main.runtime.node_relationships import (
    messageable_target_ids as node_messageable_target_ids,
)
from main.runtime.node_runner import NodeRunner
from main.runtime.node_seed_contracts import seed_drop_reason, trim_seed_contracts
from main.runtime.node_turn_controller import NodeTurnController
from main.runtime.node_wake import (
    WakeDeps,
    node_park_state,
    wake_parked_node,
)
from main.runtime.node_watchdog import NodeWatchdog
from main.runtime.react_loop import ReActToolLoop
from main.runtime.task_actor_service import TaskActorService
from main.runtime.tool_pressure_monitor import WorkerPressureMonitor
from main.service.event_registry import TaskEventRegistry
from main.service.task_distribution_error_callback import (
    TASK_DISTRIBUTION_ERROR_CALLBACK_PATH,
    normalize_task_distribution_error_payload,
    resolve_task_distribution_error_callback_token,
    resolve_task_distribution_error_callback_url,
)
from main.service.task_event_callback import (
    TASK_EVENT_BATCH_CALLBACK_PATH,
    TASK_EVENT_CALLBACK_PATH,
    normalize_task_event_payload,
    resolve_task_event_batch_callback_url,
    resolve_task_event_callback_token,
    resolve_task_event_callback_url,
)
from main.service.task_stall_callback import (
    TASK_STALL_CALLBACK_PATH,
    TASK_STALL_REASON_CANCEL_REQUESTED,
    TASK_STALL_REASON_MISSING_TASK,
    TASK_STALL_REASON_NOT_IN_PROGRESS,
    TASK_STALL_REASON_SUSPECTED_STALL,
    TASK_STALL_REASON_USER_PAUSED,
    TASK_STALL_REASON_WORKER_UNAVAILABLE,
    normalize_task_stall_payload,
    resolve_task_stall_callback_token,
    resolve_task_stall_callback_url,
    task_notice_audience_ok,
)
from main.service.task_stall_notifier import (
    TaskStallNotifier,
    effective_silence_start,
    running_tool_deadline,
    stall_bucket_minutes,
    stalled_minutes_since,
)
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    build_task_terminal_payload,
    build_terminal_output_resolver,
    collect_task_user_node_supplements,
    enrich_task_terminal_payload,
    is_allowed_callback_url,
    load_task_terminal_callback_config,
    resolve_task_terminal_callback_token,
    resolve_task_terminal_callback_url,
)
from main.service.worker_heartbeat_service_v2 import WorkerHeartbeatServiceV2
from main.storage.artifact_store import TaskArtifactStore, read_artifact_text
from main.storage.disk_guard import (
    DiskPolicies,
    configure_disk_policies,
    disk_policies,
    disk_waterline_snapshot,
    emergency_threshold_bytes,
    has_emergency_disk_budget,
)
from main.storage.fs_utils import remove_tree
from main.storage.sqlite_store import SQLiteTaskStore

_UNSET = object()
_WORKER_STATUS_STALE_AFTER_SECONDS = 15.0
_WORKER_STATUS_ACTIVE_TASK_STALE_AFTER_SECONDS = 60.0
_WORKER_STATUS_STARTING_GRACE_SECONDS = 10.0
_WORKER_STATUS_CALLBACK_RETRY_DELAYS = [0.0, 0.5, 2.0, 5.0]
_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS = 2.0
# 声明侧参数表载体的上限。常态是 0 条：只有"名字还在钉住清单里、参数表却只剩上一跳原文可取"
# 时才有条目（实例被删且没留快照、或派发字典被收窄）。超限按名字序截断——被截掉的那条下一次
# 自己回退去读工件，退化成"没有载体"，不会因此把名字挤出清单。
_DECLARATION_BACKFILL_MAX_ENTRIES = 32
# 清单渲染发生变化的那一跳，把整表原字节存成一份内容引用。它的 kind 必须与
# `task_actual_request` 不同：`_prune_stale_actual_request_artifacts` 按 kind 剪枝，同 kind
# 会被逐跳删掉，而那正是"上一跳原文"这一档会空的原因。
_PINNED_SCHEMA_BUNDLE_KIND = 'pinned_tool_schema_bundle'
# 整表按名索引的进程内缓存上限。一个键对应一份清单（32 条 ≈ 16 KB），64 份 ≈ 1 MB，
# 而健康路径本来一次工件读都不该有——这个数只是给"每跳都重读整表"留的天花板。
_PINNED_BUNDLE_INDEX_CACHE_LIMIT = 64
# 终态/失速/分发错误三条 outbox 车道共用：2.0s 在 web 侧跑模型探测或抢
# runtime.sqlite3 写锁时必然超时，而超时会让事件一路滞留到下次进程重启才补投
# （2026-09-23 task:543e0f15d798 滞留 1h39m、task:7e2a270eec34 滞留 10h16m）。
_INTERNAL_CALLBACK_TIMEOUT_SECONDS = 5.0
# 累计投递上限：达到即落 'abandoned' 留痕，避免常驻补投循环无限撞。
# 一次梯子 4 发，10 次 ≈ 2.5 轮，足够跨过一次短暂的 web 重启。
_TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT = 10
_TASK_TERMINAL_DELIVERY_MAX_INFLIGHT = 8
_WORKER_RUNTIME_REFRESH_TIMEOUT_SECONDS = 5.0
_WORKER_RUNTIME_REFRESH_POLL_SECONDS = 0.1
_WORKER_NODE_MESSAGE_TIMEOUT_SECONDS = 10.0
_WORKER_NODE_MESSAGE_POLL_SECONDS = 0.05
_WORKER_NODE_MESSAGE_FIELDS = (
    'task_id',
    'target_node_id',
    'message',
    'origin',
    'relation',
    'system_kind',
    'source_node_id',
    'verdict',
    'ref',
    'submission_id',
    'epoch_id',
    'wake',
    'held',
    'batch_id',
)
_WORKER_LEASE_ROLE = 'task_worker'
_WORKER_LEASE_TTL_SECONDS = 20.0
_TASK_SUMMARY_DEBOUNCE_SECONDS = 0.5
_TASK_SUMMARY_MAX_WAIT_SECONDS = 1.0
_TASK_SUMMARY_BATCH_WINDOW_SECONDS = 0.25
_TASK_SUMMARY_BATCH_MAX_ITEMS = 128
_TASK_SUMMARY_BATCH_MAX_BYTES = 64 * 1024
# 实时事件车道：一个事件一个 create_task 的派发方式在实盘留下 3 846 条不关的回环
# ESTABLISHED（worker→自家 web），两小时 26→3 846，最终把机器的套接字缓冲吃空
# （WinError 10055）。合并窗与批量上限把连接需求从 O(事件数) 降到 O(批次数)。
_TASK_EVENT_BATCH_WINDOW_SECONDS = 0.25
_TASK_EVENT_BATCH_MAX_ITEMS = 128
_TASK_EVENT_BATCH_MAX_BYTES = 64 * 1024
_TASK_EVENT_QUEUE_MAX_ITEMS = 4096
# 条数闸门在实盘不够用：一条 live.patch 载荷就是该任务的全量摘要（MB 级），4096 条
# 的上限允许队列驻留住几 GB。夜里的实测是 `entry_event_queue_depth` 打满 4096、
# `entry_event_dropped_total` 累计 18715 条被丢，而且没有任何"丢了哪一类"的记录
# （`delivery skipped` 是 logger.debug，搜不到不等于没发生）。
# 字节水位用**观测到的均值**估（排空时本来就算过 `encoded_size`），不在入队路径上
# 再做一次 json.dumps——那是我们刚砍掉的开销。
_TASK_EVENT_QUEUE_MAX_BYTES = 32 * 1024 * 1024
_TASK_EVENT_QUEUE_PRESSURE_ITEMS = 256
_TASK_EVENT_DROP_WARN_INTERVAL_SECONDS = 60.0
_TASK_EVENT_UNKNOWN_ITEM_BYTES = 8 * 1024
# 只要出现过 MB 级载荷的类型，就每拍都算一遍字节水位：三条 16 MB 的摘要就能把队列
# 撑到危险，而它远没碰到 256 条的危险区闸门。夜里那次打满就是这个形状。
_TASK_EVENT_HEAVY_ITEM_BYTES = 1024 * 1024
# 载荷本身就是"该任务当前全量状态"的事件类型：队列里同任务的旧一份可以直接被新的覆盖。
# 只列 live.patch——node.patch/terminal/model.call 的载荷是增量或事实行，覆盖会丢账。
_TASK_EVENT_COALESCED_EVENT_TYPES = frozenset({'task.live.patch'})
# 共享回调客户端的连接池档位。`keepalive_expiry` 必须**小于** uvicorn 的
# `timeout_keep_alive`（默认 5s）：谁先关空闲连接是可定的，反之连接被对端悄悄 FIN 掉、
# 池里还留着一条"可用"的 socket，下一次复用才撞上，表现是偶发一次失败的 POST。
# `max_connections` 只是兜底闸：实盘并发只有 1–3，卡到 32 就已经是事故，
# 宁可排队到 `pool` 超时丢一条事件，也不许再攒出一条没人认领的 socket。
_CALLBACK_POOL_MAX_CONNECTIONS = 32
_CALLBACK_POOL_MAX_KEEPALIVE_CONNECTIONS = 4
_CALLBACK_POOL_KEEPALIVE_EXPIRY_SECONDS = 2.0
# 回调投递异常按类型计数，限流打一行。这条车道原来的失败全部落在 `logger.debug`，
# 实盘按 "delivery skipped" 搜整窗口得 0 命中——那是级别问题不是没发生。
_CALLBACK_ERROR_WARN_INTERVAL_SECONDS = 60.0
# 回环连接普查：超过这一条就限流打一行存活任务名分组，用来点名占连接的车道。
_LOOPBACK_CENSUS_WARN_CONNECTIONS = 2000
_LOOPBACK_CENSUS_INTERVAL_SECONDS = 60.0
# 取证车道（默认关死）：数据根里放一个 `loop-census.on` 才开，逐点名 loop 上的任务。
# 用途是「worker 心跳正常但命令车道不消费」这类现场：光看 worker_status 分不出
# 事件循环死了还是某条车道任务不在了，而这条车道本身也顺便回答这个问题。
_LOOP_CENSUS_MARKER_NAME = 'loop-census.on'
_LOOP_CENSUS_INTERVAL_SECONDS = 20.0
# `schedule_wake` 的扫描节拍。5 秒是"唤醒到点后最多晚 5 秒"与"每分钟扫 12 次窄表"之间的取值；
# 表是按 wake_at_ms 索引的点查，空表时这条查询不碰任何胖行（见 main/storage/sqlite_store 的 timer 段）。
_NODE_WAKE_TIMER_POLL_SECONDS = 5.0
# 用户/父节点/前门给“已提交、正等验收”的执行节点发消息：消息落箱但不唤醒，
# 工具回执必须说清消费时机，否则发送方会以为节点下一回合一定会读到。
_WAITING_ACCEPTANCE_DELIVERY_NOTICE = (
    'The target execution node has submitted its output and is awaiting acceptance. '
    'The message was delivered to its mailbox but will not be consumed while the acceptance '
    'is in progress. If the acceptance passes, the node terminates without consuming it; if '
    'the submission is rejected and the node is reactivated, the node consumes it in its next round.'
)
_LOOP_CENSUS_SAMPLE_TASKS = 6
_LOOP_CENSUS_TOP_SITES = 10
# gc 全量扫一次要持 GIL 一两秒，所以池榜每 5 拍（约 100 秒）才点一次，其余拍报空列表。
_LOOP_CENSUS_POOL_SCAN_EVERY = 5
_TASK_SUMMARY_RECONCILE_IDLE_SECONDS = 15.0
_TASK_DELETE_CONFIRM_TTL_SECONDS = 600.0
# 停滞事件的事实部分要扫同伴任务的调度计数：扫得越多越全，但这条只在真要发事件时跑，
# 所以按"够判读就停"取小值——8 个占位任务、24 个任务的上限扫描、3 帧现场。
_STALL_DETAIL_OCCUPIER_LIMIT = 8
_STALL_DETAIL_OCCUPIER_SCAN_LIMIT = 24
_STALL_DETAIL_FRAME_LIMIT = 3
# 删除前等待暂停排空的上限：到期未排空按 task_still_stopping 拒绝，
# 调用方可在暂停生效后重试；避免无限等待离线/卡死的排空。
_DELETE_PAUSE_DRAIN_TIMEOUT_SECONDS = 10.0
# web 自身 stdout 落点（negi_bootstrap 把它交给子进程句柄）的长跑封顶：正文写在
# 一个继承来的 append 句柄上，改名换不掉，所以这里只就地截尾。实盘 187 MB/23h
# 的那次是 QQ 桥重连风暴把 loguru 全量 traceback 灌进来的量级。
_CONSOLE_LOG_CAP_BYTES = 100 * 1024 * 1024
_CONSOLE_LOG_KEEP_BYTES = 25 * 1024 * 1024
_CONSOLE_LOG_CAP_MIN_INTERVAL_SECONDS = 3600.0
_WORKER_STATE_STARTING = 'starting'
_WORKER_STATE_ONLINE = 'online'
_WORKER_STATE_STALE = 'stale'
_WORKER_STATE_STOPPED = 'stopped'
_WORKER_STATE_OFFLINE = 'offline'
_WORKER_STATUS_TERMINAL_STATES = frozenset({'stopped', 'offline', 'dead'})
# 性能历史读窗口：判"任务是否因性能停滞"要看过去值，而 worker_status 只有
# 最新一行。采样侧每 15s 落一行（见 worker_heartbeat_service_v2），这里只做
# 有界读取 + 降采样渲染，返回体量必须落在上下文内联闸门以下。
_PERF_REPORT_DEFAULT_WINDOW_MINUTES = 10.0
_PERF_REPORT_MAX_WINDOW_MINUTES = 1440.0
_PERF_REPORT_MAX_ROWS = 6000
# 单样本窗口没有可测步长；按采样周期记一份占用时长，否则 1 个 throttled 样本
# 会算出 restricted_share=0%，读起来正好是反的。
_PERF_SAMPLE_INTERVAL_FALLBACK_SECONDS = 15.0
# 序列行数上限：超过 40 桶时把步长放大到 window/40，所以报告长度与窗口无关。
_PERF_REPORT_MAX_BUCKETS = 40
_PERF_TIER_ORDER = ('normal', 'easing', 'throttled', 'critical')
# 心跳里每个组最多带几个成员的明细：载荷要小，够判断「负载摊开了没有、谁在冷却」即可。
_MODEL_ROUTE_SNAPSHOT_MAX_MEMBERS = 12
# 每个统计轴的 (报告标签, 载荷字段, 单位)；序列与统计行共用这一份词表，
# 避免"少一列"只在渲染处暴露。
_PERF_STAT_AXES = (
    ('cpu', 'machine_pressure_cpu_percent', 'percent'),
    ('mem', 'machine_pressure_memory_percent', 'percent'),
    ('disk', 'machine_pressure_disk_busy_percent', 'percent'),
    ('tq_run', 'tool_queue_running_count', 'count'),
    ('tq_wait', 'tool_queue_waiting_count', 'count'),
    ('nq_run', 'node_queue_running_count', 'count'),
    ('nq_wait', 'node_queue_waiting_count', 'count'),
    ('nq_frozen', 'node_queue_frozen_count', 'count'),
    # 闸口排队：节点在物化上下文之前就卡在这里，所以它是"等请求位"的真数；
    # nq_wait 量的是过闸之后等模型 permit，闸收紧时恒 0（4000 拍里 3986 拍为 0）。
    ('gate_wait', 'entry_gate_queued_total', 'count'),
    ('gate_slots', 'entry_gate_limits_total', 'count'),
    ('tq_oldest', 'worker_execution_oldest_wait_ms', 'ms'),
    ('lag', 'tool_pressure_event_loop_lag_ms', 'ms'),
    ('db_wait', 'sqlite_write_wait_ms', 'ms'),
    ('db_query', 'sqlite_query_latency_ms', 'ms'),
    ('wq', 'tool_pressure_writer_queue_depth', 'count'),
    ('disk_free', 'machine_disk_free_bytes', 'bytes'),
    ('tasks', 'active_task_count', 'count'),
)
# 序列只画这 8 个轴（其余留给统计行）：一行一桶，列宽固定，模型不用对齐表头。
# 闸位没有富余时 nq_wait 恒 0（见上面的注释），所以序列这一格给 gate_wait。
_PERF_SERIES_LABELS = ('cpu', 'mem', 'disk', 'tq_run', 'tq_wait', 'nq_run', 'gate_wait', 'lag')
_PERF_AXIS_KEY = {label: key for label, key, _kind in _PERF_STAT_AXES}
_PERF_AXIS_KIND = {label: kind for label, _key, kind in _PERF_STAT_AXES}
_PERF_GROUPED_AXES = (
    ('Queues', ('tq_run', 'tq_wait', 'tq_oldest', 'nq_run', 'nq_wait', 'nq_frozen', 'gate_wait', 'gate_slots', 'tasks')),
    ('Machine', ('cpu', 'mem', 'disk', 'disk_free')),
    ('Library', ('db_wait', 'db_query', 'wq', 'lag')),
)


def _perf_number(value: Any, *, drop_negative: bool = False) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric:
        return None
    if drop_negative and numeric < 0.0:
        return None
    return numeric


def _perf_percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * max(0.0, min(1.0, fraction))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return float(ordered[lower]) + (float(ordered[upper]) - float(ordered[lower])) * (position - lower)


def _perf_axis_text(label: str, kind: str, stats: dict[str, float]) -> str:
    if kind == 'percent':
        return f"{label} max={stats['max']:.0f}% p50={stats['p50']:.0f}%"
    if kind == 'ms':
        return f"{label} max={_perf_duration_text(stats['max'])} p50={_perf_duration_text(stats['p50'])}"
    if kind == 'bytes':
        return f"{label} min={_perf_bytes_text(stats['min'])} last={_perf_bytes_text(stats['last'])}"
    return f"{label} max={stats['max']:.0f} p50={stats['p50']:.1f}"


def _perf_duration_text(milliseconds: float) -> str:
    value = max(0.0, float(milliseconds or 0.0))
    if value >= 60000.0:
        return f'{value / 60000.0:.1f}m'
    if value >= 1000.0:
        return f'{value / 1000.0:.1f}s'
    return f'{value:.0f}ms'


def _perf_seconds_text(seconds: float) -> str:
    value = max(0.0, float(seconds or 0.0))
    if value >= 60.0:
        return f'{value / 60.0:.1f}m'
    return f'{value:.0f}s'


def _perf_bytes_text(value: float) -> str:
    numeric = float(value or 0.0)
    if numeric >= 1024 ** 3:
        return f'{numeric / 1024 ** 3:.1f}G'
    if numeric >= 1024 ** 2:
        return f'{numeric / 1024 ** 2:.0f}M'
    return f'{numeric / 1024:.0f}K'


def _perf_clock_text(stamp: datetime) -> str:
    return stamp.astimezone().strftime('%H:%M:%S')


def _perf_tiers_text(tier_seconds: dict[str, float]) -> str:
    ordered = sorted(
        tier_seconds.items(),
        key=lambda item: _PERF_TIER_ORDER.index(item[0]) if item[0] in _PERF_TIER_ORDER else 99,
    )
    return ' '.join(f'{state}={_perf_seconds_text(seconds)}' for state, seconds in ordered)
_TASK_RECOVERY_NOTICE_KEY = 'recovery_notice'
_TASK_RECOVERY_NOTICE_TEXT = '本任务遇到异常停止，已回退到稳定步骤继续。'
_TASK_RUNTIME_V3_MARKER = '.task-runtime-v3'
_NODE_FIXED_BUILTIN_TOOL_NAMES = NODE_FIXED_BUILTIN_TOOL_NAMES


def _prepare_task_runtime_v3_root(
    *,
    store_path: Path,
    files_base_dir: Path,
    artifact_dir: Path,
    event_history_dir: Path,
) -> None:
    runtime_root = store_path.parent
    marker_path = runtime_root / _TASK_RUNTIME_V3_MARKER
    if marker_path.exists():
        return
    # 只有"这块根上确实没有库"时才清残留。库存在而标记不在，是两种正常操作留下的样子：
    # 从备份恢复 runtime.sqlite3、或把数据根指向一个已有库的目录。旧写法在这里会先 unlink
    # 库文件与 -wal/-shm、再 rmtree 掉 tasks/artifacts/event-history —— 2026-10-03 一份用
    # sqlite3 backup API 造的 1.88 GB 生产副本就是这么被构造一次删成 364 KB / 0 行的。
    # 库在就只补标记，一个字节都不删。
    if store_path.exists() and int(store_path.stat().st_size or 0) > 0:
        logger.warning(
            'task runtime v3 marker missing but store exists: keeping {} untouched',
            store_path,
        )
        runtime_root.mkdir(parents=True, exist_ok=True)
        marker_path.write_text('task-runtime-v3\n', encoding='utf-8')
        return
    runtime_root.mkdir(parents=True, exist_ok=True)
    for candidate in (
        store_path,
        store_path.with_name(f'{store_path.name}-wal'),
        store_path.with_name(f'{store_path.name}-shm'),
    ):
        try:
            if candidate.exists():
                candidate.unlink()
        except FileNotFoundError:
            continue
    for directory in (files_base_dir, artifact_dir, event_history_dir):
        try:
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            logger.debug('task runtime v3 cleanup skipped for {}', directory)
    marker_path.write_text('task-runtime-v3\n', encoding='utf-8')


class ResourceDeleteBlockedError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        resource_kind: str,
        resource_id: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.payload = {
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
            'resource_kind': str(resource_kind or '').strip(),
            'resource_id': str(resource_id or '').strip(),
            'usage': dict(usage or {}),
        }


class ResourceMutationBlockedError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        resource_kind: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.payload = {
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
            'resource_kind': str(resource_kind or '').strip(),
            'resource_id': str(resource_id or '').strip(),
            'details': dict(details or {}),
        }


class CallbackUrlNotAllowedError(RuntimeError):
    """Raised when an outbound internal-callback URL fails URL policy checks."""


def _callback_host_for_log(url: str) -> str:
    try:
        parsed = urlparse(url)
        return str(parsed.hostname or parsed.netloc or url).strip()
    except ValueError:
        return str(url or '').strip()


class MainRuntimeService:
    def __init__(
        self,
        *,
        chat_backend: ChatBackend,
        app_config: Any | None = None,
        store_path: Path | str | None = None,
        files_base_dir: Path | str | None = None,
        artifact_dir: Path | str | None = None,
        governance_store_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
        resource_manager=None,
        execution_model_refs: list[str] | None = None,
        acceptance_model_refs: list[str] | None = None,
        default_max_depth: int = 10,
        hard_max_depth: int = 4,
        max_iterations: int | None | object = _UNSET,
        execution_max_iterations: int | None | object = _UNSET,
        acceptance_max_iterations: int | None | object = _UNSET,
        execution_mode: str = 'embedded',
        worker_id: str | None = None,
    ) -> None:
        self._chat_backend = chat_backend
        self._app_config = app_config
        # 任务临时目录（temp/tasks）默认跟随数据根；显式 workspace_root（测试/嵌入式
        # 脚本）优先于数据根，把临时目录隔离到独立目录，避免泄漏到进程 cwd。
        self._workspace_root_override = (
            Path(workspace_root).expanduser().resolve(strict=False) if workspace_root is not None else None
        )
        resolved_max_iterations = self._normalize_optional_limit(max_iterations, default=16)
        resolved_execution_max_iterations = self._normalize_optional_limit(
            execution_max_iterations,
            default=resolved_max_iterations,
        )
        resolved_acceptance_max_iterations = self._normalize_optional_limit(
            acceptance_max_iterations,
            default=resolved_execution_max_iterations,
        )
        normalized_mode = str(execution_mode or 'embedded').strip().lower() or 'embedded'
        if normalized_mode not in {'embedded', 'web', 'worker'}:
            normalized_mode = 'embedded'
        self.execution_mode = normalized_mode
        self.worker_id = str(worker_id or (new_worker_id() if normalized_mode == 'worker' else '')).strip()
        resolved_store_path = resolve_data_path(store_path, default='.g3ku/main-runtime/runtime.sqlite3')
        resolved_files_base_dir = resolve_data_path(files_base_dir, default='.g3ku/main-runtime/tasks')
        resolved_artifact_dir = resolve_data_path(artifact_dir, default='.g3ku/main-runtime/artifacts')
        event_history_settings = self._event_history_settings(app_config)
        # 磁盘治理（P0）：从 config.main_runtime.disk_guard 注入进程级策略单例。
        configure_disk_policies(self._disk_guard_policies(app_config))
        duplicate_precheck_settings = self._duplicate_precheck_settings(app_config)
        self._duplicate_precheck_llm_review_enabled = bool(duplicate_precheck_settings.get('llm_review_enabled', True))
        configured_event_history_dir = str(event_history_settings.get('dir') or '').strip()
        resolved_event_history_dir = Path(
            configured_event_history_dir or (resolved_store_path.parent / 'event-history')
        )
        _prepare_task_runtime_v3_root(
            store_path=resolved_store_path,
            files_base_dir=resolved_files_base_dir,
            artifact_dir=resolved_artifact_dir,
            event_history_dir=resolved_event_history_dir,
        )
        self.runtime_debug_recorder = RuntimeDebugRecorder()
        self.store = SQLiteTaskStore(
            resolved_store_path,
            debug_recorder=self.runtime_debug_recorder,
            event_history_dir=resolved_event_history_dir,
            event_history_enabled=bool(event_history_settings.get('enabled', True)),
            event_history_archive_encoding=str(event_history_settings.get('archive_encoding') or 'gzip'),
        )
        self.file_store = TaskFileStore(resolved_files_base_dir)
        self.artifact_store = TaskArtifactStore(artifact_dir=resolved_artifact_dir, store=self.store)
        # 任务删除前产出导出的持久目录（永久保留，不参与磁盘治理）。
        self._deliverables_dir = resolved_store_path.parent / 'deliverables'
        self.content_store = ContentNavigationService(
            workspace=data_root(),
            artifact_store=self.artifact_store,
            artifact_lookup=self.store,
        )
        self.registry = TaskEventRegistry()
        self.log_service = TaskLogService(
            store=self.store,
            file_store=self.file_store,
            registry=self.registry,
            content_store=self.content_store,
            debug_recorder=self.runtime_debug_recorder,
            event_history_enabled=bool(event_history_settings.get('enabled', True)),
            live_patch_persist_window_ms=int(event_history_settings.get('live_patch_persist_window_ms') or 0),
        )
        self.log_service.add_live_snapshot_publisher(self._publish_live_snapshot)
        self.log_service.add_summary_metric_reporter(self._increment_summary_stat)
        self.task_stall_notifier = TaskStallNotifier(service=self)
        self.log_service.add_task_visible_output_listener(self.task_stall_notifier.reset_visible_output)
        self.log_service.add_task_terminal_listener(self._cleanup_terminal_task_temp_dir_if_empty)
        # 磁盘治理（P0）：终态即清中间产物（temp/tasks 草稿 + 中间 artifact）。
        self._terminal_cleanup_inflight: set[str] = set()
        self._terminal_cleanup_inflight_guard = threading.Lock()
        self.log_service.add_task_terminal_listener(self._cleanup_terminal_task_intermediates)
        self.log_service.add_task_terminal_listener(self.task_stall_notifier.terminal_task)
        self.query_service = TaskQueryServiceV2(store=self.store, file_store=self.file_store, log_service=self.log_service, debug_recorder=self.runtime_debug_recorder)
        self.governance_store = GovernanceStore(
            resolve_data_path(governance_store_path, default='.g3ku/main-runtime/governance.sqlite3')
        )
        # exec 命令白名单与操作者审批（worker 等待、web 裁决，经 governance sqlite 跨进程）。
        self.exec_approvals = ExecApprovalService(self.governance_store)
        self.resource_registry = MainRuntimeResourceRegistry(workspace_root=Path.cwd(), store=self.governance_store, resource_manager=resource_manager)
        self.policy_engine = MainRuntimePolicyEngine(store=self.governance_store, resource_registry=self.resource_registry)
        self._resource_manager = resource_manager
        self._resource_tree_state_cache: dict[str, dict[str, str]] | None = None
        self._resource_tree_state_checked_at: float = 0.0
        self._resource_tree_state_poll_interval_ms = self._resource_reload_poll_interval_ms(app_config)
        self.memory_manager = None
        self._default_max_depth = max(0, int(default_max_depth or 0))
        # `hard_max_depth` 是机器上限，`default_max_depth` 是未显式指定时的默认值。
        # 两者在配置里可能倒挂（默认 10 / 硬顶 4），取大者保持"硬顶不小于默认"的
        # 不变式；真正的裁剪只在 `_clamp_depth` 一处做，不在别处再抄一份。
        self._hard_max_depth = max(self._default_max_depth, int(hard_max_depth or self._default_max_depth))
        self._max_task_nodes = max(
            1,
            int(
                getattr(getattr(app_config, 'main_runtime', None), 'max_task_nodes', 200) or 200
            ),
        )
        self._node_tool_timeout_ceiling_seconds = max(
            1.0,
            float(
                getattr(
                    getattr(app_config, 'main_runtime', None),
                    'node_tool_timeout_ceiling_seconds',
                    900.0,
                )
                or 900.0
            ),
        )
        self._node_watchdog_interval_seconds = max(
            1.0,
            float(
                getattr(
                    getattr(app_config, 'main_runtime', None),
                    'node_watchdog_interval_seconds',
                    30.0,
                )
                or 30.0
            ),
        )
        self._node_watchdog_stall_seconds = max(
            60.0,
            float(
                getattr(
                    getattr(app_config, 'main_runtime', None),
                    'node_watchdog_stall_seconds',
                    600.0,
                )
                or 600.0
            ),
        )
        # P6：节点看门狗（结构判停）。与任务级失速通知并存：后者只发会话回调，
        # 前者把节点转成「暂停·异常」并升级父节点。
        self.node_watchdog = NodeWatchdog(
            service=self,
            interval_seconds=self._node_watchdog_interval_seconds,
            stall_seconds=self._node_watchdog_stall_seconds,
        )
        self._builtin_tool_cache: dict[str, Tool] | None = None
        self._node_context_selection_cache: dict[tuple[str, str], dict[str, Any]] = {}
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(app_config)
        adaptive_budget_settings = self._adaptive_tool_budget_settings(app_config)
        sample_seconds = float(adaptive_budget_settings['sample_seconds'])
        gate_stale = float(adaptive_budget_settings['gate_stale_after_seconds'])
        if gate_stale > 0.0:
            # Never let the staleness fail-open threshold sit below the sampling cadence.
            gate_stale = max(gate_stale, sample_seconds * 3.0)
        self._pressure_gate_stale_after_seconds = gate_stale
        self._pressure_gate_close_on_machine_critical = bool(adaptive_budget_settings['gate_close_on_machine_critical'])
        node_dispatch_limits = self._node_dispatch_concurrency_settings(app_config)
        execution_runtime_enabled = self.execution_mode == 'worker'
        execution_max_concurrency = (
            app_config.get_role_max_concurrency('execution')
            if app_config is not None and hasattr(app_config, 'get_role_max_concurrency')
            else None
        )
        acceptance_max_concurrency = (
            app_config.get_role_max_concurrency('inspection')
            if app_config is not None and hasattr(app_config, 'get_role_max_concurrency')
            else None
        )
        react_loop = ReActToolLoop(
            chat_backend=chat_backend,
            log_service=self.log_service,
            max_iterations=resolved_max_iterations,
            parallel_tool_calls_enabled=parallel_enabled,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        react_loop._runtime_config_refresh_for_retry_invalidation = (
            lambda: self.ensure_runtime_config_current(force=False, reason='provider_retry_invalidation')
        )
        self._chat_backend = chat_backend
        self.model_key_concurrency_controller = ModelKeyConcurrencyController(
            resolve_model_limits=self._resolve_model_limit_payload,
        ) if execution_runtime_enabled else None
        # 全局负载均衡器与并发控制器同生命周期：同一个 worker 进程内的所有任务、节点和
        # 两条车道共享组状态。controller 缺失（embedded/web 角色）时 balancer 为 None，
        # 选择层退化成「无并发计数的直连绑定」，不让节点因为没有 balancer 而报错。
        self.model_load_balancer = (
            ModelLoadBalancer(
                permit_source=self.model_key_concurrency_controller,
                resolve_quota_buckets=self._resolve_quota_buckets,
            )
            if self.model_key_concurrency_controller is not None
            else None
        )
        self.node_turn_controller = NodeTurnController(
            model_concurrency_controller=self.model_key_concurrency_controller,
            balancer=self.model_load_balancer,
            gate_supplier=self._node_turn_gate_allowed,
        ) if execution_runtime_enabled and self.model_key_concurrency_controller is not None else None
        if self.model_key_concurrency_controller is not None and self.node_turn_controller is not None:
            self.model_key_concurrency_controller.configure(
                resolve_model_limits=self._resolve_model_limit_payload,
                on_availability_changed=self.node_turn_controller.poke,
            )
        react_loop._model_concurrency_controller = self.model_key_concurrency_controller
        react_loop._node_turn_controller = self.node_turn_controller
        adaptive_budget_enabled = bool(adaptive_budget_settings.get('enabled')) and self.execution_mode == 'worker'
        self.adaptive_tool_budget_controller = AdaptiveToolBudgetController(
            normal_limit=int(adaptive_budget_settings['normal_limit']),
            throttled_limit=int(adaptive_budget_settings['throttled_limit']),
            critical_limit=int(adaptive_budget_settings['critical_limit']),
            step_up=int(adaptive_budget_settings['step_up']),
        ) if adaptive_budget_enabled else None
        react_loop._adaptive_tool_budget_controller = self.adaptive_tool_budget_controller
        react_loop._model_visible_tool_schema_selector = self._select_model_visible_tool_schema_payload
        # 重印跳的整表重写发生在 react_loop（那里才知道新名单），选择器只知道"帧里有没有 prior"。
        react_loop._pinned_tool_schema_bundle_persister = self._persist_pinned_tool_schema_bundle
        # 冲动调用投递与入参暂存：节点道只有这条循环能拿到 service，载体（node.metadata）
        # 也在这里读写，跳序与时序才不会分叉。
        react_loop._tool_impulse_service = self
        self._pending_task_delete_confirmations: dict[str, dict[str, Any]] = {}
        self._react_loop = react_loop
        initial_execution_routes = self._initial_model_routes(app_config, 'execution')
        initial_acceptance_routes = self._initial_model_routes(app_config, 'inspection')
        self.node_runner = NodeRunner(
            store=self.store,
            log_service=self.log_service,
            react_loop=react_loop,
            tool_provider=self._tool_provider,
            execution_model_refs=list(execution_model_refs or ['execution']),
            acceptance_model_refs=list(acceptance_model_refs or execution_model_refs or ['inspection']),
            execution_model_routes=initial_execution_routes,
            acceptance_model_routes=initial_acceptance_routes,
            execution_max_iterations=resolved_execution_max_iterations,
            acceptance_max_iterations=resolved_acceptance_max_iterations,
            max_parallel_child_pipelines=max_parallel_child_pipelines,
            execution_max_concurrency=execution_max_concurrency,
            acceptance_max_concurrency=acceptance_max_concurrency,
            context_enricher=self._enrich_node_messages,
            context_preparer=self._prepare_node_context_selection,
            context_finalizer=self._clear_node_context_selection,
            workspace_root_getter=lambda: self._workspace_root(),
            seed_contract_metadata_builder=self.build_node_seed_metadata,
        )
        self._apply_model_route_plans(initial_execution_routes, initial_acceptance_routes, revision=0)
        self.node_runner._tool_snapshot_supplier = self._tool_watchdog_snapshot_supplier
        # 节点道 skill 名单的每跳取数口：react_loop 无 service 引用，只能由这里递进去
        # （与 `_tool_snapshot_supplier` 同一条 side-channel）。
        self.node_runner._skill_roster_supplier = self._node_skill_roster_supplier
        # 节点头部快照与一次性通知的 exec 策略取数口：节点不持 service 引用，
        # 与 skill 取数口同一条 side-channel；每跳实时读治理记录（无缓存）。
        self.node_runner._exec_runtime_policy_supplier = self._current_exec_runtime_policy_payload
        # 节点侧 `send_message` 的投递口：NodeRunner 没有 service 引用，走同一条 side-channel。
        self.node_runner.node_message_delivery_callback = self._deliver_node_message_from_node
        # 节点侧 `manage_child_nodes` 的控制口：暂停/恢复/终止需要 dispatcher、暂停账本
        # 与投递通道，全部在这一层。
        self.node_runner.manage_child_nodes_callback = self._manage_child_nodes_from_node
        # P1「终局必上报」的唯一出口：执行节点终局 → 父节点信箱 `child_final`。
        self.node_runner.child_final_sink = self._deliver_child_final_message
        # 异常暂停 → 父节点信箱 `child_abnormal_pause`（high 优先级，必唤醒）。
        self.node_runner.abnormal_pause_sink = self._deliver_child_abnormal_pause
        # 验收车道（交接单 + 三档裁决）的结构性消息与 `send_message` 共用同一条投递
        # 实现，确保它们也带机械推导的 kind/priority（直写通知表会留下无类型行）。
        self.node_runner.mailbox_delivery_sink = self._deliver_structural_node_message
        self.node_runner._tool_default_timeout_seconds = self._tool_default_timeout_seconds(app_config)
        self.node_runner._max_task_nodes = self._max_task_nodes
        self.node_runner._node_tool_timeout_ceiling_seconds = self._node_tool_timeout_ceiling_seconds
        self.task_actor_service = TaskActorService(
            store=self.store,
            log_service=self.log_service,
            node_runner=self.node_runner,
            stall_notifier=self.task_stall_notifier,
            # 节点回合闸改由压力状态机实时驱动：worker 模式以前把这两个 limit 置 None
            # （等于完全不闸），于是一次 resume/pickup 放出的每个执行器都会先物化自己的
            # 上下文，工具与模型槽位的自适应根本来不及拦。现在天花板取配置值，容量随
            # critical/throttled/磁盘紧急收缩。
            node_dispatch_execution_limit=int(node_dispatch_limits['execution']),
            node_dispatch_inspection_limit=int(node_dispatch_limits['inspection']),
            node_dispatch_entry_budget=self.adaptive_tool_budget_controller,
        )
        self.global_scheduler = GlobalScheduler(
            runner=self.task_actor_service,
            max_concurrent_tasks=None if execution_runtime_enabled else 4,
            per_task_limit=1,
        )
        # web 角色只有控制面：worker 在线时 `run_task` 在这里必须拒绝，否则会与
        # worker 并发驱动同一棵树。判据绑 worker 在线状态，离线时保留本地执行语义
        # （没有 worker 就没有可并发的另一个驱动者）。
        def _web_role_blocks_local_actors() -> bool:
            if self.execution_mode != 'web':
                return False
            try:
                return self.worker_state() != _WORKER_STATE_OFFLINE
            except Exception:
                return True

        self.task_actor_service.web_role_blocked = _web_role_blocks_local_actors
        self.task_actor_service.node_watchdog = self.node_watchdog

        def _notify_wait_cycle(**kwargs: Any) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(self._notify_wait_cycle(**kwargs), name='wait-cycle-notice')

        self.task_actor_service.wait_cycle_notifier = _notify_wait_cycle
        self.task_actor_service.interrupted_task_requeue_callback = (
            lambda task_id: asyncio.get_running_loop().call_soon(
                lambda normalized_task_id=str(task_id or '').strip(): asyncio.create_task(
                    self._requeue_interrupted_task(normalized_task_id),
                    name=f'task-interrupted-requeue:{normalized_task_id}',
                )
            )
        )
        self.tool_pressure_monitor = WorkerPressureMonitor(
            controller=self.adaptive_tool_budget_controller,
            store=self.store,
            sample_seconds=float(adaptive_budget_settings['sample_seconds']),
            recover_window_seconds=float(adaptive_budget_settings['recover_window_seconds']),
            warn_consecutive_samples=int(adaptive_budget_settings['warn_consecutive_samples']),
            safe_consecutive_samples=int(adaptive_budget_settings['safe_consecutive_samples']),
            pressure_snapshot_stale_after_seconds=float(adaptive_budget_settings['pressure_snapshot_stale_after_seconds']),
            event_loop_warn_ms=float(adaptive_budget_settings['event_loop_warn_ms']),
            event_loop_safe_ms=float(adaptive_budget_settings['event_loop_safe_ms']),
            event_loop_critical_ms=float(adaptive_budget_settings['event_loop_critical_ms']),
            writer_queue_warn=int(adaptive_budget_settings['writer_queue_warn']),
            writer_queue_safe=int(adaptive_budget_settings['writer_queue_safe']),
            writer_queue_critical=int(adaptive_budget_settings['writer_queue_critical']),
            sqlite_write_wait_warn_ms=float(adaptive_budget_settings['sqlite_write_wait_warn_ms']),
            sqlite_write_wait_safe_ms=float(adaptive_budget_settings['sqlite_write_wait_safe_ms']),
            sqlite_write_wait_critical_ms=float(adaptive_budget_settings['sqlite_write_wait_critical_ms']),
            sqlite_query_warn_ms=float(adaptive_budget_settings['sqlite_query_warn_ms']),
            sqlite_query_safe_ms=float(adaptive_budget_settings['sqlite_query_safe_ms']),
            sqlite_query_critical_ms=float(adaptive_budget_settings['sqlite_query_critical_ms']),
            machine_cpu_warn_percent=float(adaptive_budget_settings['machine_cpu_warn_percent']),
            machine_cpu_safe_percent=float(adaptive_budget_settings['machine_cpu_safe_percent']),
            machine_cpu_critical_percent=float(adaptive_budget_settings['machine_cpu_critical_percent']),
            machine_memory_warn_percent=float(adaptive_budget_settings['machine_memory_warn_percent']),
            machine_memory_safe_percent=float(adaptive_budget_settings['machine_memory_safe_percent']),
            machine_memory_critical_percent=float(adaptive_budget_settings['machine_memory_critical_percent']),
            machine_disk_busy_warn_percent=float(adaptive_budget_settings['machine_disk_busy_warn_percent']),
            machine_disk_busy_safe_percent=float(adaptive_budget_settings['machine_disk_busy_safe_percent']),
            machine_disk_busy_critical_percent=float(adaptive_budget_settings['machine_disk_busy_critical_percent']),
            process_cpu_warn_ratio=float(adaptive_budget_settings['process_cpu_warn_ratio']),
            process_cpu_safe_ratio=float(adaptive_budget_settings['process_cpu_safe_ratio']),
            max_pressure_dwell_seconds=float(adaptive_budget_settings['max_pressure_dwell_seconds']),
            max_tool_wait_ms=float(adaptive_budget_settings['max_tool_wait_ms']),
            local_recovery_enabled=bool(adaptive_budget_settings['local_recovery_enabled']),
            # 回合闸的上游限流轴：读负载均衡器已有的衰减 429 惩罚与滚动 RPM，不另起计数。
            rate_limit_observer=(
                self.model_load_balancer.rate_pressure
                if getattr(self, 'model_load_balancer', None) is not None
                else None
            ),
            # 回环连接读数：内部回调车道占用的 ESTABLISHED 条数，套接字耗尽的提前信号。
            loop_observer=self.loopback_pressure_snapshot,
            disk_watermark_paths=[
                str(data_root()),
                str(resolved_store_path.parent),
                str(resolved_artifact_dir),
            ],
        ) if self.adaptive_tool_budget_controller is not None else None
        if self.tool_pressure_monitor is not None:
            # 磁盘治理（P1）：紧急态边沿钩子（monitor 采样线程内调用，必须立即返回；
            # 内部经 call_soon_threadsafe 调度回事件循环）。
            self.tool_pressure_monitor.set_disk_emergency_hooks(
                enter=lambda: self._schedule_loop_task(self._auto_pause_all_due_to_disk_emergency),
                exit_=lambda: self._schedule_loop_task(self._disk_emergency_released),
            )
        self.node_runner._adaptive_tool_budget_controller = self.adaptive_tool_budget_controller
        self.worker_heartbeat_service = WorkerHeartbeatServiceV2(
            store=self.store,
            scheduler=self.global_scheduler,
            execution_mode=self.execution_mode,
            worker_id=self.worker_id or 'worker',
            publish_status=self._publish_worker_status_from_any_thread,
            pressure_snapshot_supplier=self._tool_pressure_snapshot,
            debug_snapshot_supplier=lambda: {
                'recent_long_blocks': self.runtime_debug_recorder.snapshot(),
                'worst_long_blocks': self.runtime_debug_recorder.worst_snapshot(),
                # 事件写失败与库层写失败计数随心跳入库，排障信号（磁盘满/静默写丢事件）无新增查询路径。
                'event_write_failures': int(self.log_service.event_write_failure_count() or 0),
                'sqlite_write_failures': dict(self.store.write_failure_counts() or {}),
            },
            lease_heartbeat=self._renew_worker_lease_from_thread,
        )
        self._started = False
        # _runtime_loop 装的是 AgentLoop（frontdoor/心跳桥要读它的 sessions、
        # web_session_heartbeat 等属性）；要往事件循环里
        # 线程安全投递的读者一律走 _event_loop，两者不得混用同一个字段。
        self._runtime_loop = None
        self._event_loop = None
        self._watchdog_snapshot_flights: dict[str, asyncio.Task] = {}
        self._mem_probe = None
        self._loop_census_task = None
        # 节点自设唤醒的扫描节拍（worker 车道）。
        self._node_wake_timer_task: asyncio.Task[Any] | None = None
        self._worker_lease_takeover = False
        self._worker_lease_acquired = False
        self._command_poller_task: asyncio.Task[Any] | None = None
        self._worker_heartbeat_task: asyncio.Task[Any] | None = None
        # 磁盘治理（P1）：对账循环、紧急态自动暂停互斥与边沿状态。
        self._task_disk_reconcile_task: asyncio.Task[Any] | None = None
        # 分发驱动器接管对账循环（子树屏障不能只靠驱动器自己活着）。
        self._terminal_outbox_reconcile_task: asyncio.Task[Any] | None = None
        self._disk_emergency_pause_lock = asyncio.Lock()
        self._disk_emergency_alerted = False
        self._task_terminal_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_stall_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_distribution_error_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_worker_status_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_summary_delivery_task: asyncio.Task[Any] | None = None
        self._task_event_pending: list[dict[str, Any]] = []
        self._task_event_flush_task: asyncio.Task[Any] | None = None
        self._task_event_stats: dict[str, float] = {
            'task_event_queued_count': 0.0,
            'task_event_batch_request_count': 0.0,
            'task_event_batch_item_count': 0.0,
            'task_event_dropped_count': 0.0,
            'task_event_single_fallback_count': 0.0,
            'task_event_coalesced_count': 0.0,
            'callback_delivery_error_count': 0.0,
            'task_event_dropped_coalesced_count': 0.0,
            'task_event_dropped_other_count': 0.0,
            'task_event_delivery_dropped_count': 0.0,
        }
        # 排空时学到的每类平均字节数与丢弃归属：字节水位靠估，不在入队路径序列化。
        self._task_event_avg_item_bytes: dict[str, int] = {}
        self._task_event_dropped_by_type: dict[str, int] = {}
        # 投递侧的丢弃归属：所有回调目标都失败过一次才算真丢，队列溢出丢的是另一本账。
        self._task_event_delivery_dropped_by_type: dict[str, int] = {}
        self._task_event_delivery_logged_mono = 0.0
        self._task_event_heavy_types: set[str] = set()
        self._task_event_drop_logged_mono = 0.0
        self._loopback_census_logged_mono = 0.0
        self._callback_delivery_errors: dict[str, int] = {}
        self._callback_error_logged_mono = 0.0
        self._callback_client: httpx.AsyncClient | None = None
        self._pending_task_summaries: dict[str, dict[str, Any]] = {}
        self._task_summary_flush_tasks: dict[str, asyncio.Task[Any]] = {}
        self._last_summary_payloads: dict[str, dict[str, Any]] = {}
        self._task_summary_stats: dict[str, float] = {
            'task_summary_dirty_count': 0.0,
            'task_summary_flush_count': 0.0,
            'task_summary_skip_unchanged_count': 0.0,
            'task_summary_outbox_write_count': 0.0,
            'task_summary_batch_request_count': 0.0,
            'task_summary_batch_item_count': 0.0,
        }
        if self.execution_mode == 'worker':
            self.log_service.add_task_terminal_listener(self._enqueue_task_terminal_callback)

    @staticmethod
    def _bool_env(name: str, *, default: bool) -> bool:
        raw = str(os.getenv(name, '') or '').strip().lower()
        if not raw:
            return bool(default)
        return raw not in {'0', 'false', 'no', 'off'}

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        if self._runtime_loop is None:
            self._runtime_loop = asyncio.get_running_loop()
        self._event_loop = asyncio.get_running_loop()
        if self.execution_mode == 'worker':
            # 探针要在租约与任务 pickup 之前起：要观测的就是恢复风暴那一刻的分配驻留。
            self._mem_probe = start_mem_probe(runtime_dir=self.store.path.parent)
            self._acquire_worker_lease_or_raise()
            self.worker_heartbeat_service.start_background()
        self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        self._record_resource_tree_state()
        if self.execution_mode in {'embedded', 'worker'}:
            shutdown_paused_task_ids = {
                str(item.get('ref_id') or ''): str(item.get('entry_key') or '')
                for item in self.store.list_shutdown_pause_entries(kind='task')
                if str(item.get('ref_id') or '').strip()
            }
            for task in self.store.list_tasks():
                if str(task.status or '').strip().lower() != 'in_progress':
                    # 终态任务的节点行不会再被写：投影戳与节点戳一致就说明派生已是最新，
                    # 启动不再整任务重读重写（实盘 1,912/1,912 行一致，旧写法每次启动
                    # 为此读掉 nodes 全表 486 MB 并重写 1,912 行投影）。
                    if self.store.count_stale_task_node_projections(task.task_id):
                        # 整任务投影重建是同步 sqlite 读 + 逐行派生：冷页下实盘 4,851 ms，
                        # 而启动循环跑在事件循环线程上，这段时间里任务树推送与命令轮询全部停摆。
                        await asyncio.to_thread(
                            self.log_service.sync_task_read_models,
                            task.task_id,
                            externalize_execution_trace=False,
                        )
                    # Self-heal: a terminal task must not retain orphaned in_progress
                    # nodes. This only flips bookkeeping status and never deletes
                    # transcripts/artifacts; see log_service._sweep_residual_nodes_locked.
                    self.log_service.sweep_residual_nodes(task.task_id)
                    # A terminal/deleted task cannot be resumed; retire its
                    # shutdown-pause ledger row so it does not linger and get
                    # reconciled against a later task id reuse.
                    self.store.mark_shutdown_pause_entry_consumed(kind='task', ref_id=task.task_id)
                    continue
                await asyncio.to_thread(
                    self.log_service.sync_task_read_models,
                    task.task_id,
                    externalize_execution_trace=False,
                )
                if bool(task.is_paused) or bool(task.pause_requested):
                    if task.task_id not in shutdown_paused_task_ids:
                        # A user/agent-initiated pause must survive restarts.
                        continue
                    # Cleanly paused by a graceful process exit: resume silently
                    # (no abnormal-stop recovery notice) and run again.
                    await self.resume_task(task.task_id)
                    self.store.mark_shutdown_pause_entry_consumed(kind='task', ref_id=task.task_id)
                    continue
                self._recover_interrupted_task(task.task_id)
                await self.global_scheduler.enqueue_task(task.task_id)
            # Retire ledger rows that reference tasks which no longer exist, so
            # the registry cannot grow stale or collide with reused task ids.
            known_task_ids = {str(task.task_id or '').strip() for task in self.store.list_tasks() if str(task.task_id or '').strip()}
            for item in self.store.list_shutdown_pause_entries(kind='task'):
                ref_id = str(item.get('ref_id') or '').strip()
                if ref_id and ref_id not in known_task_ids:
                    self.store.mark_shutdown_pause_entry_consumed(kind='task', ref_id=ref_id)
            self.task_stall_notifier.bootstrap_running_tasks()
            self.node_watchdog.bootstrap_running_tasks()
            await self._reconcile_wait_all_batches()
        if self.execution_mode in {'embedded', 'worker'}:
            # 磁盘治理（P1）：任务大小对账循环（每小时增量纠偏 + 终态即时对账）。
            if self._task_disk_reconcile_task is None or self._task_disk_reconcile_task.done():
                self._task_disk_reconcile_task = asyncio.create_task(
                    self._task_disk_reconcile_loop(),
                    name=f'main-runtime-disk-reconcile:{self.worker_id or self.execution_mode}',
                )
            # 终态 outbox 的周期重投：宿主循环原先是分发对账的搭车者，分发下线后
            # 单独保留这一半（失败投递否则要等到下次重启才会重放）。
            if self._terminal_outbox_reconcile_task is None or self._terminal_outbox_reconcile_task.done():
                self._terminal_outbox_reconcile_task = asyncio.create_task(
                    self._terminal_outbox_reconcile_loop(),
                    name=f'main-runtime-terminal-outbox-reconcile:{self.worker_id or self.execution_mode}',
                )
        if self.execution_mode == 'worker':
            if self.tool_pressure_monitor is not None:
                self.tool_pressure_monitor.start()
            self._start_worker_loops()
            self._schedule_pending_task_summary_callbacks()
            self._schedule_pending_task_worker_status_callbacks()
            self._schedule_pending_task_terminal_callbacks()
            self._schedule_pending_task_stall_callbacks()
            self._schedule_pending_task_distribution_error_callbacks()

    def _start_worker_loops(self) -> None:
        if self._command_poller_task is None or self._command_poller_task.done():
            self._command_poller_task = asyncio.create_task(self._worker_command_loop(), name=f'main-runtime-command-poller:{self.worker_id or "worker"}')
        if self._worker_heartbeat_task is None or self._worker_heartbeat_task.done():
            self._worker_heartbeat_task = asyncio.create_task(self._worker_heartbeat_loop(), name=f'main-runtime-worker-heartbeat:{self.worker_id or "worker"}')
        if self._loop_census_task is None or self._loop_census_task.done():
            self._loop_census_task = asyncio.create_task(self._loop_census_loop(), name=f'main-runtime-loop-census:{self.worker_id or "worker"}')
        if self._node_wake_timer_task is None or self._node_wake_timer_task.done():
            self._node_wake_timer_task = asyncio.create_task(
                self._node_wake_timer_loop(),
                name=f'main-runtime-node-wake-timer:{self.worker_id or "worker"}',
            )

    async def _node_wake_timer_loop(self) -> None:
        """`schedule_wake` 的节拍：到点的唤醒投一条 `wake_timer` 通知，不在协程里等。

        期限存在 `task_node_timers` 里而不是存在 `asyncio.sleep` 里，就是为了重启也认：实盘那条
        `exec Start-Sleep 5400`（task:4555863a6a49 的报时节点）08:30 设的 10:00 被 09:02 的重启
        打断后，恢复的模型重新发了一次完整 5400 秒，下一次到点被推成 10:33。这里没有"剩余
        时长"可丢——扫表时 `wake_at_ms <= now` 就是一条待投递。
        """
        while True:
            try:
                await asyncio.sleep(_NODE_WAKE_TIMER_POLL_SECONDS)
                fired = await self.sweep_due_node_wakes()
                if fired:
                    logger.info(f'node wake timer sweep: fired={fired}')
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 节拍死掉等于所有已设唤醒静默失效
                logger.warning(f'node wake timer sweep failed: {exc!r}')
                await asyncio.sleep(_NODE_WAKE_TIMER_POLL_SECONDS * 4)

    async def sweep_due_node_wakes(self) -> int:
        """投出所有到点的节点唤醒，返回处理条数。

        一条唤醒的三种结局都改得了状态且不重投：

        - 节点/任务已终态或节点行没了 ⇒ 删行（否则终态任务的表会一直涨）。
        - 节点或任务被暂停 ⇒ 落箱但不叫醒（`wake=False`）。叫醒会顺手清掉操作员按下的暂停位，
          这条判据与 `node_mailbox.delivery_defers_wake` 给人发往暂停节点的消息同一档。
        - 投递本身抛异常 ⇒ **不**退回领取位。留着凭证意味着下一次重试要等过
          `_WAKE_CLAIM_STALE_MS`（2 分钟），这就是失败道唯一的退避——磁盘满一类的故障下，
          5 秒一拍会把清扫变成热循环。
        """
        claim_token = str(self.worker_id or 'worker').strip() or 'worker'
        due = self.store.claim_due_task_node_timers(
            now_ms=wall_now_ms(),
            claimed_by=claim_token,
        )
        fired = 0
        for timer in list(due or []):
            fired += 1
            task_id = str(timer.task_id or '').strip()
            node_id = str(timer.node_id or '').strip()
            task = self.store.get_task(task_id)
            node = self.store.get_node(node_id)
            if task is None or node is None or str(node.status or '').strip().lower() in {'success', 'failed'}:
                self.store.delete_task_node_timer(task_id=task_id, node_id=node_id)
                continue
            if str(task.status or '').strip().lower() in {'success', 'failed', 'canceled'}:
                self.store.delete_task_node_timer(task_id=task_id, node_id=node_id)
                continue
            paused = (
                bool(getattr(task, 'is_paused', False))
                or bool(getattr(task, 'pause_requested', False))
                or bool(getattr(node, 'is_paused', False))
                or bool(getattr(node, 'pause_requested', False))
            )
            current_ms = wall_now_ms()
            late_ms = max(0, current_ms - int(timer.wake_at_ms or 0))
            try:
                receipt = await self.deliver_node_message(
                    task_id=task_id,
                    target_node_id=node_id,
                    message=self._node_wake_notice_text(
                        note=str(timer.note or ''),
                        due_ms=int(timer.wake_at_ms or 0),
                        now_ms=current_ms,
                        late_ms=late_ms,
                        paused=paused,
                    ),
                    origin='node',
                    system_kind=KIND_WAKE_TIMER,
                    source_node_id=node_id,
                    wake=not paused,
                )
            except Exception as exc:  # noqa: BLE001 - 凭证留着，下一拍过 stale 窗口后重试
                logger.warning(
                    'node wake timer delivery failed: task={} node={} wake_at_ms={} error={!r}',
                    task_id,
                    node_id,
                    timer.wake_at_ms,
                    exc,
                )
                continue
            self.store.delete_task_node_timer(task_id=task_id, node_id=node_id)
            logger.info(
                'node wake timer fired: task={} node={} due={} late_ms={} wake={} status={}',
                task_id,
                node_id,
                iso_from_ms(int(timer.wake_at_ms or 0)),
                late_ms,
                (not paused),
                str(receipt.get('status') or '') if isinstance(receipt, dict) else '',
            )
        return fired

    @staticmethod
    def _node_wake_notice_text(
        *,
        note: str,
        due_ms: int,
        now_ms: int,
        late_ms: int,
        paused: bool,
    ) -> str:
        """唤醒通知的模型可见正文：英文，并说清"这是你自己设的"。"""
        lines = [
            f'Scheduled wake-up for: {note or "(no note was given)"}',
            f'You armed it for {iso_from_ms(due_ms)}. It is now {iso_from_ms(now_ms)}.',
        ]
        if late_ms >= 60_000:
            lines.append(
                f'It fired {late_ms // 1000}s after its due time — the runtime was paused, '
                'busy, or restarted in between. Re-check the clock before you act on any '
                'deadline math built on the original schedule.'
            )
        if paused:
            lines.append(
                'This node is paused, so the notice is only filed here: nothing woke up. '
                'It reaches the model when the operator resumes this node.'
            )
        lines.append(
            'To keep a schedule running, arm the next wake-up now; this one is spent.'
        )
        return '\n'.join(lines)

    async def _requeue_interrupted_task(self, task_id: str) -> None:
        """引擎级中断任务的尽力即时重排（进程仍活着时）。

        延迟 1s 再入队：若中断源仍在（杂散取消未消失），限制「取消→重排→
        取消」的回转速度，避免热循环。进程正处于退出收尾时调度器已关闭或
        即将关闭，入队自然无效——任务由下一个 worker 启动时的
        ``_recover_interrupted_task`` 恢复重排兜底（2026-09-16
        task:eacd0f0467b7 事故：引擎中断被终态化成 canceled，任务永久丢失）。
        """
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return
        try:
            await asyncio.sleep(1.0)
            if self.global_scheduler.closed:
                return
            latest = self.store.get_task(normalized_task_id)
            if latest is None:
                return
            if str(latest.status or '').strip().lower() != 'in_progress':
                return
            if bool(latest.cancel_requested) or bool(latest.pause_requested) or bool(latest.is_paused):
                return
            await self.global_scheduler.enqueue_task(normalized_task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug('interrupted task requeue skipped: task={}', normalized_task_id)

    def _recover_interrupted_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        root = self.store.get_node(task.root_node_id)
        if root is None:
            self.log_service.mark_task_failed(task.task_id, reason='missing root node during recovery')
            return

        # 仍存在的验收节点指针必须保留：终态节点由下一次提交的
        # `_reset_terminal_acceptance_for_reverify` 作废重验；在这里清空指针会让
        # 最终验收路径看不到旧节点并新建第二颗（task:a78c14859578 的重复验收）。
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        acceptance_node_id = str(final_acceptance.node_id or '').strip()
        acceptance_node = self.store.get_node(acceptance_node_id) if acceptance_node_id else None
        preserve_final_acceptance = acceptance_node is not None

        self.store.update_task(
            task.task_id,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'pause_requested': False,
                    'is_paused': False,
                    'finished_at': None,
                    'final_output': '',
                    'final_output_ref': '',
                    'failure_reason': '',
                    'updated_at': now_iso(),
                    'metadata': self._sanitize_recovered_task_metadata(
                        record.metadata,
                        preserve_final_acceptance=preserve_final_acceptance,
                    ),
                }
            ),
        )

        self.log_service.update_task_runtime_meta(
            task.task_id,
            last_visible_output_at=self._stall_now_iso(),
            last_stall_notice_bucket_minutes=0,
        )
        runtime_state = self.log_service.read_runtime_state(task.task_id) or {}
        frames = [dict(item) for item in list(runtime_state.get('frames') or []) if isinstance(item, dict)]
        active_node_ids = [str(item) for item in list(runtime_state.get('active_node_ids') or []) if str(item or '').strip()]
        runnable_node_ids = [str(item) for item in list(runtime_state.get('runnable_node_ids') or []) if str(item or '').strip()]
        waiting_node_ids = [str(item) for item in list(runtime_state.get('waiting_node_ids') or []) if str(item or '').strip()]
        if not frames:
            # 恢复语义:制造出的帧只表示「可运行、待调度」,不得标记 active——
            # 真正的执行资格由随后的调度/执行轮次授予。否则 task_progress
            # 会把恢复后尚未获得执行的任务直接渲染成「运行中」。
            frames = [self.log_service._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase='before_model')]
            active_node_ids = []
            runnable_node_ids = [root.node_id]
            waiting_node_ids = []
        self.log_service.replace_runtime_frames(
            task.task_id,
            frames=frames,
            active_node_ids=active_node_ids,
            runnable_node_ids=runnable_node_ids,
            waiting_node_ids=waiting_node_ids,
            publish_snapshot=False,
        )
        self.log_service.sync_task_read_models(task.task_id, externalize_execution_trace=False)
        self.log_service.refresh_task_view(task.task_id, mark_unread=True)
        # P8：崩溃前发出、但父节点信箱里已经不存在的异常暂停升级，恢复时补发一次。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self.resend_lost_abnormal_pause_escalations(task.task_id),
            name=f'abnormal-pause-resend:{task.task_id}',
        )

    @staticmethod
    def _sanitize_recovered_task_metadata(metadata: dict[str, Any], *, preserve_final_acceptance: bool) -> dict[str, Any]:
        cleaned = dict(metadata or {})
        cleaned.pop('final_execution_output', None)
        cleaned[_TASK_RECOVERY_NOTICE_KEY] = _TASK_RECOVERY_NOTICE_TEXT
        final_acceptance = normalize_final_acceptance_metadata(cleaned.get('final_acceptance'))
        if final_acceptance.required or str(final_acceptance.prompt or '').strip():
            cleaned['final_acceptance'] = {
                'required': bool(final_acceptance.required),
                'prompt': str(final_acceptance.prompt or ''),
                'node_id': str(final_acceptance.node_id or '').strip() if preserve_final_acceptance else '',
                'status': str(final_acceptance.status or 'pending').strip().lower() if preserve_final_acceptance else 'pending',
            }
        else:
            cleaned.pop('final_acceptance', None)
        return cleaned

    async def _worker_command_loop(self) -> None:
        idle_delays = [0.25, 0.5, 1.0, 2.0]
        idle_index = 0
        while True:
            try:
                commands = self.store.claim_pending_task_commands(
                    worker_id=self.worker_id or 'worker',
                    claimed_at=now_iso(),
                    limit=20,
                )
                if not commands:
                    await asyncio.sleep(idle_delays[min(idle_index, len(idle_delays) - 1)])
                    idle_index = min(idle_index + 1, len(idle_delays) - 1)
                    continue
                idle_index = 0
                for command in commands:
                    await self._process_worker_command(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                idle_index = 0
                await asyncio.sleep(0.5)

    def _loop_census_marker_path(self) -> Path:
        return Path(self.store.path.parent) / _LOOP_CENSUS_MARKER_NAME

    @staticmethod
    def _task_suspend_site(task: asyncio.Task) -> str:
        if task.done():
            return 'done'
        frame = getattr(task.get_coro(), 'cr_frame', None)
        if frame is None:
            return 'not-started'
        code = frame.f_code
        return f'{os.path.basename(code.co_filename)}:{frame.f_lineno} in {code.co_name}'

    @staticmethod
    def _census_referrer_label(obj: Any) -> str:
        name = type(obj).__name__
        if name == 'method':
            # 挂起中的 proactor 操作就写在这里：recv_into / sendall / connect 三种
            # 形状给出的修法完全不同（等响应 / 请求体没写完 / 没连上），只报类型名
            # 说不清是哪一种。
            return f'method<{getattr(obj, "__name__", "?")}>'
        if name != 'dict':
            return name
        import gc

        try:
            holders = [
                type(other).__name__
                for other in gc.get_referrers(obj)
                if not isinstance(other, (list, tuple, dict))
            ]
        except Exception:
            holders = []
        return f'dict<{holders[0] if holders else "?"}>'

    @classmethod
    def _census_referrer_trail(cls, obj: Any, *, hops: int = 3) -> str:
        """沿引用者往上走 hops 层，每层记类型名（字典记成 `dict<持有者>`）。"""
        import gc

        trail: list[str] = []
        current: Any = obj
        seen: set[int] = {id(obj)}
        for _ in range(hops):
            try:
                referrers = [
                    ref for ref in gc.get_referrers(current)
                    if id(ref) not in seen and not isinstance(ref, (list, tuple))
                ]
            except Exception:
                break
            if not referrers:
                break
            current = referrers[0]
            seen.add(id(current))
            trail.append(cls._census_referrer_label(current))
        return ' <- '.join(trail) or '(none)'

    @classmethod
    def _loopback_socket_census(cls, port: int) -> dict[str, Any]:
        """一次 gc 扫同时回答三件事，用来给"挂在自家 web 端口上的连接"归因。

        ① 这些 socket 由谁持有（引用者链，走到 transport 之上）；② 进程里每个 httpcore
        池有多少连接、多少被租出、这个池本身挂在哪个对象上；③ AsyncClient 实例有几个。
        实盘 19:5x 的形状：OS 22 条 ESTABLISHED，socket 全被 `_ProactorSocketTransport`
        持有，而 5 个池加起来只有 1 条连接 ⇒ 池已经丢了这些连接、transport 却从没 close，
        所以"租着没还"和"野 fd"两种说法都不成立，得连池的持有者一起看才能说出是谁的。
        gc 全量扫一次要持 GIL 一两秒，因此只在标记文件在的时候按 5 拍一次。
        """
        import gc
        import socket as socket_module

        result: dict[str, Any] = {'sockets': 0, 'owners': {}, 'pools': [], 'clients': 0}
        if not port:
            return result
        try:
            objects = gc.get_objects()
        except Exception:
            return result
        sockets: list[Any] = []
        pools: list[dict[str, Any]] = []
        clients = 0
        for item in objects:
            try:
                type_name = type(item).__name__
                if type_name == 'AsyncClient':
                    clients += 1
                    continue
                if type_name == 'AsyncConnectionPool':
                    connections = list(item.connections)
                    leased = 0
                    for connection in connections:
                        try:
                            if not bool(connection.is_available()):
                                leased += 1
                        except Exception:
                            continue
                    pools.append({'conns': len(connections), 'leased': leased, 'trail': cls._census_referrer_trail(item)})
                    continue
                if not isinstance(item, socket_module.socket):
                    continue
                peer = item.getpeername()
            except Exception:
                continue
            if not peer or int(peer[1] or 0) != int(port):
                continue
            sockets.append(item)
        owners: dict[str, int] = {}
        for sock in sockets:
            trail = cls._census_referrer_trail(sock)
            owners[trail] = owners.get(trail, 0) + 1
        result['sockets'] = len(sockets)
        result['owners'] = dict(sorted(owners.items(), key=lambda item: -item[1])[:4])
        result['pools'] = pools[:8]
        result['clients'] = clients
        return result

    async def _loop_census_loop(self) -> None:
        """标记文件在的时候把 loop 上的任务点名打一行；不在就只是睡着。

        要分的现场是「事件循环停了」和「循环在转但某条车道的任务不在了」：
        这条车道自己出现在输出里，就证明前者不成立，嫌疑只剩后者。
        """

        ticks = 0
        while True:
            await asyncio.sleep(_LOOP_CENSUS_INTERVAL_SECONDS)
            try:
                if not self._loop_census_marker_path().exists():
                    continue
                ticks += 1
                lane: dict[str, Any] = {}
                if ticks % _LOOP_CENSUS_POOL_SCAN_EVERY == 0:
                    lane = self._loopback_socket_census(self._loopback_web_port())
                tasks = list(asyncio.all_tasks())
                prefixes: dict[str, int] = {}
                site_counts: dict[str, int] = {}
                poller_site = 'missing'
                for task in tasks:
                    name = str(task.get_name() or '')
                    key = name.split(':', 1)[0] or '(unnamed)'
                    prefixes[key] = prefixes.get(key, 0) + 1
                    if 'command-poller' in name:
                        poller_site = self._task_suspend_site(task)
                    # 挂起点榜：任务名前缀只说"谁建的"，这个才说"卡在什么调用上"。
                    site = self._task_suspend_site(task)
                    site_counts[site] = site_counts.get(site, 0) + 1
                sites = [
                    f'{task.get_name()}@{self._task_suspend_site(task)}'
                    for task in tasks[:_LOOP_CENSUS_SAMPLE_TASKS]
                ]
                logger.warning(
                    'loop census: worker={} poller={} total={} lane={} prefixes={} top_sites={} sample={}',
                    self.worker_id or 'worker',
                    poller_site,
                    len(tasks),
                    lane,
                    dict(sorted(prefixes.items(), key=lambda item: -item[1])[:8]),
                    dict(sorted(site_counts.items(), key=lambda item: -item[1])[:_LOOP_CENSUS_TOP_SITES]),
                    sites,
                )
            except Exception:
                pass

    async def _worker_heartbeat_loop(self) -> None:
        await self.worker_heartbeat_service.run_forever()

    def _publish_worker_status_from_any_thread(self, item: dict[str, Any]) -> None:
        loop = self._event_loop
        payload = dict(item or {})
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(
            lambda: self.publish_worker_status_event(item=payload, bridge=False)
        )

    async def _process_worker_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get('command_id') or '').strip()
        command_type = str(command.get('command_type') or '').strip()
        task_id = self.normalize_task_id(str(command.get('task_id') or '').strip())
        payload = dict(command.get('payload') or {}) if isinstance(command.get('payload'), dict) else {}
        success = False
        error_text = ''
        result_payload: dict[str, Any] | None = None
        try:
            if command_type == 'create_task':
                task = self.get_task(task_id)
                if task is not None and not bool(task.is_paused):
                    await self.global_scheduler.enqueue_task(task.task_id)
                success = True
            elif command_type == 'resume_task':
                if task_id:
                    await self.resume_task(task_id)
                success = True
            elif command_type == 'pause_task':
                if task_id:
                    task = self.get_task(task_id)
                    # 暂停意图由持久标志承载：被恢复清空后，还在队列里的 pause 命令
                    # 就是上一个进程的死账，落回来会把刚自动恢复的任务再打成暂停。
                    if task is None or not (bool(task.pause_requested) or bool(task.is_paused)):
                        result_payload = {'skipped': 'pause_intent_cleared'}
                    else:
                        await self.pause_task(task_id)
                success = True
            elif command_type == 'cancel_task':
                if task_id:
                    await self.cancel_task(task_id)
                success = True
            elif command_type == 'pause_node':
                if task_id:
                    result_payload = await self._apply_pause_node_command(
                        task_id,
                        node_ids=list(payload.get('node_ids') or []),
                        cascade=bool(payload.get('cascade', False)),
                        reason=str(payload.get('reason') or 'manual'),
                        remark=str(payload.get('remark') or ''),
                    )
                success = True
            elif command_type == 'resume_node':
                if task_id:
                    result_payload = await self._apply_resume_node_command(task_id, node_ids=list(payload.get('node_ids') or []), force=True)
                success = True
            elif command_type == 'fail_node':
                if task_id:
                    result_payload = await self._apply_fail_node_command(
                        task_id,
                        node_ids=list(payload.get('node_ids') or []),
                        reason=str(payload.get('reason') or payload.get('remark') or ''),
                        force=True,
                    )
                success = True
            elif command_type == 'send_node_message':
                target_node_id = self.normalize_node_id(str(payload.get('target_node_id') or '').strip())
                delivery = {
                    key: payload[key]
                    for key in _WORKER_NODE_MESSAGE_FIELDS
                    if key in payload
                }
                delivery['task_id'] = task_id
                delivery['target_node_id'] = target_node_id
                if task_id and target_node_id:
                    result_payload = await self.deliver_node_message(**delivery)
                    success = True
                else:
                    error_text = 'send_node_message_missing_target'
            elif command_type == 'refresh_runtime_config':
                changed = self.ensure_runtime_config_current(
                    force=True,
                    reason=str(payload.get('reason') or 'worker_command_refresh').strip() or 'worker_command_refresh',
                )
                result_payload = {
                    'changed': bool(changed),
                    'applied_revision': int(getattr(self, '_runtime_model_revision', 0) or 0),
                    'applied_config_mtime_ns': int(self._config_mtime_ns()),
                    'worker_id': str(self.worker_id or 'worker'),
                    'worker_pid': int(os.getpid()),
                }
                success = True
            else:
                error_text = f'unsupported_command:{command_type}'
        except Exception as exc:
            error_text = str(exc)
        finally:
            if command_id:
                self.store.finish_task_command(
                    command_id,
                    finished_at=now_iso(),
                    success=success,
                    error_text=error_text,
                    result=result_payload,
                )

    def _enqueue_task_command(
        self,
        *,
        command_type: str,
        task_id: str | None,
        session_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        command_id = new_command_id()
        self.store.enqueue_task_command(
            command_id=command_id,
            task_id=task_id,
            session_id=str(session_id or 'web:shared').strip() or 'web:shared',
            command_type=str(command_type or '').strip(),
            created_at=now_iso(),
            payload=dict(payload or {}),
        )
        return command_id

    @staticmethod
    def _config_mtime_ns() -> int:
        try:
            return int(Path(get_config_path()).stat().st_mtime_ns)
        except Exception:
            return 0

    @staticmethod
    def _lease_expiry_from(updated_at: str, *, ttl_seconds: float = _WORKER_LEASE_TTL_SECONDS) -> str:
        parsed = MainRuntimeService._parse_worker_timestamp(updated_at)
        if parsed is None:
            return now_iso()
        return (parsed + timedelta(seconds=max(1.0, float(ttl_seconds or _WORKER_LEASE_TTL_SECONDS)))).astimezone().isoformat(timespec='seconds')

    def _worker_lease_payload(self, *, heartbeat_at: str) -> dict[str, Any]:
        return {
            'workspace': str(Path.cwd()),
            'worker_id': str(self.worker_id or 'worker'),
            'worker_pid': int(os.getpid()),
            'heartbeat_at': str(heartbeat_at or ''),
            'execution_mode': str(self.execution_mode or ''),
            'takeover': bool(self._worker_lease_takeover),
        }

    def _acquire_worker_lease_or_raise(self) -> None:
        if self.execution_mode != 'worker':
            return
        started_at = now_iso()
        lease = self.store.acquire_worker_lease(
            role=_WORKER_LEASE_ROLE,
            worker_id=str(self.worker_id or 'worker'),
            holder_pid=int(os.getpid()),
            acquired_at=started_at,
            heartbeat_at=started_at,
            expires_at=self._lease_expiry_from(started_at),
            payload=self._worker_lease_payload(heartbeat_at=started_at),
        )
        if bool(lease.get('acquired')):
            self._worker_lease_takeover = bool(lease.get('takeover'))
            self._worker_lease_acquired = True
            return
        holder = str(lease.get('worker_id') or '').strip() or 'unknown'
        expires_at = str(lease.get('expires_at') or '').strip()
        raise RuntimeError(f'worker_lease_unavailable:{holder}:{expires_at}')

    def _renew_worker_lease_from_thread(self, heartbeat_at: str, payload: dict[str, Any]) -> None:
        if self.execution_mode != 'worker' or not self._worker_lease_acquired:
            return
        merged_payload = {
            **self._worker_lease_payload(heartbeat_at=heartbeat_at),
            'status_payload': dict(payload or {}),
        }
        self.store.renew_worker_lease(
            role=_WORKER_LEASE_ROLE,
            worker_id=str(self.worker_id or 'worker'),
            heartbeat_at=heartbeat_at,
            expires_at=self._lease_expiry_from(heartbeat_at),
            payload=merged_payload,
        )

    async def request_worker_runtime_refresh(
        self,
        *,
        reason: str,
        timeout_s: float = _WORKER_RUNTIME_REFRESH_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        if self.execution_mode != 'web':
            changed = self.ensure_runtime_config_current(force=True, reason=reason)
            return {
                'changed': bool(changed),
                'worker_refresh_acked': True,
                'worker_id': str(self.worker_id or ''),
                'worker_pid': int(os.getpid()),
                'applied_config_mtime_ns': int(self._config_mtime_ns()),
            }
        command_id = self._enqueue_task_command(
            command_type='refresh_runtime_config',
            task_id=None,
            session_id='web:shared',
            payload={
                'reason': str(reason or '').strip() or 'runtime_refresh',
                'expected_config_mtime_ns': int(self._config_mtime_ns()),
            },
        )
        deadline = time.monotonic() + max(0.1, float(timeout_s or _WORKER_RUNTIME_REFRESH_TIMEOUT_SECONDS))
        while True:
            current = self.store.get_task_command(command_id)
            if current and str(current.get('status') or '').strip().lower() in {'completed', 'failed'}:
                if str(current.get('status') or '').strip().lower() == 'failed':
                    raise RuntimeError(str(current.get('error_text') or 'worker_runtime_refresh_failed').strip() or 'worker_runtime_refresh_failed')
                result = dict(current.get('result') or {}) if isinstance(current.get('result'), dict) else {}
                return {
                    'worker_refresh_acked': True,
                    'command_id': command_id,
                    **result,
                }
            if time.monotonic() >= deadline:
                raise TimeoutError('worker_runtime_refresh_timeout')
            await asyncio.sleep(_WORKER_RUNTIME_REFRESH_POLL_SECONDS)

    def enqueue_worker_runtime_refresh(self, *, reason: str) -> dict[str, object]:
        normalized_reason = str(reason or '').strip() or 'runtime_refresh'
        if self.execution_mode != 'web':
            changed = self.ensure_runtime_config_current(force=True, reason=normalized_reason)
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': True,
                'worker_refresh_command_id': '',
                'worker_refresh_status': 'completed',
                'reason': normalized_reason,
                'changed': bool(changed),
                'worker_id': str(self.worker_id or ''),
                'worker_pid': int(os.getpid()),
                'applied_config_mtime_ns': int(self._config_mtime_ns()),
            }
        command_id = self._enqueue_task_command(
            command_type='refresh_runtime_config',
            task_id=None,
            session_id='web:shared',
            payload={
                'reason': normalized_reason,
                'expected_config_mtime_ns': int(self._config_mtime_ns()),
            },
        )
        return {
            'worker_refresh_requested': True,
            'worker_refresh_acked': False,
            'worker_refresh_command_id': command_id,
            'worker_refresh_status': 'pending',
            'reason': normalized_reason,
        }

    def get_task_command_status(self, command_id: str) -> dict[str, object] | None:
        normalized = str(command_id or '').strip()
        if not normalized:
            return None
        current = self.store.get_task_command(normalized)
        if not current:
            return None
        return dict(current)

    def _build_task_record(
        self,
        *,
        task: str,
        session_id: str,
        max_depth: int | None,
        title: str | None,
        metadata: dict[str, Any] | None,
    ) -> tuple[TaskRecord, NodeRecord]:
        prompt = str(task or '').strip()
        if not prompt:
            raise ValueError('task must not be empty')
        effective_max_depth = self._clamp_depth(max_depth)
        task_id = new_task_id()
        root_node_id = new_node_id()
        now = now_iso()
        task_metadata = self._normalize_task_metadata(
            session_id=session_id,
            metadata=metadata,
            task_prompt=prompt,
        )
        record = TaskRecord(
            task_id=task_id,
            session_id=str(session_id or 'web:shared').strip() or 'web:shared',
            title=(str(title or '').strip() or prompt[:60] or task_id),
            user_request=prompt,
            status='in_progress',
            root_node_id=root_node_id,
            max_depth=effective_max_depth,
            cancel_requested=False,
            pause_requested=False,
            is_paused=False,
            is_unread=True,
            brief_text='',
            created_at=now,
            updated_at=now,
            finished_at=None,
            final_output='',
            failure_reason='',
            token_usage=TokenUsageSummary(tracked=True),
            metadata=task_metadata,
        )
        root_goal = str(task_metadata.get('core_requirement') or prompt).strip() or prompt
        root = NodeRecord(
            node_id=root_node_id,
            task_id=task_id,
            parent_node_id=None,
            root_node_id=root_node_id,
            depth=0,
            node_kind='execution',
            status='in_progress',
            goal=root_goal,
            prompt=prompt,
            input=prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=0 < effective_max_depth,
            created_at=now,
            updated_at=now,
            token_usage=TokenUsageSummary(tracked=True),
            token_usage_by_model=[],
            metadata={
                'execution_policy': dict(task_metadata.get('execution_policy') or {}),
            },
        )
        return record, root

    async def create_task(
        self,
        task: str,
        *,
        session_id: str = 'web:shared',
        max_depth: int | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        await self.startup()
        if self.execution_mode == 'web':
            self._assert_worker_available()
        record, root = self._build_task_record(
            task=task,
            session_id=session_id,
            max_depth=max_depth,
            title=title,
            metadata=metadata,
        )
        record, root = self.log_service.initialize_task(record, root)
        final_acceptance = normalize_final_acceptance_metadata((record.metadata or {}).get('final_acceptance'))
        if final_acceptance.required and not str(final_acceptance.node_id or '').strip():
            acceptance = self.node_runner.create_acceptance_node(
                task=record,
                accepted_node=root,
                goal=f'最终验收:{root.goal}',
                acceptance_prompt=final_acceptance.prompt,
                parent_node_id=root.node_id,
                metadata={'final_acceptance': True},
            )
            self.log_service.update_task_metadata(
                record.task_id,
                lambda current_metadata: {
                    **dict(current_metadata or {}),
                    'final_acceptance': {
                        **normalize_final_acceptance_metadata(
                            (current_metadata or {}).get('final_acceptance')
                        ).model_dump(mode='json'),
                        'node_id': acceptance.node_id,
                        'status': 'pending',
                    },
                },
                mark_unread=True,
            )
            record = self.store.get_task(record.task_id) or record
            root = self.store.get_node(root.node_id) or root
        self.log_service.update_task_runtime_meta(
            record.task_id,
            task_temp_dir=str(self._task_temp_dir(record.task_id)),
        )
        if self.execution_mode in {'embedded', 'worker'}:
            await self.global_scheduler.enqueue_task(record.task_id)
        else:
            self._enqueue_task_command(
                command_type='create_task',
                task_id=record.task_id,
                session_id=record.session_id,
                payload={
                    'task_id': record.task_id,
                    'session_id': record.session_id,
                },
            )
        return self.store.get_task(record.task_id) or record

    def _node_subtree(self, task_id: str, node_id: str, *, include_root: bool = True) -> list[NodeRecord]:
        nodes = [node for node in self.store.list_nodes(task_id) if str(node.task_id or '').strip() == str(task_id or '').strip()]
        by_parent: dict[str, list[NodeRecord]] = {}
        for item in nodes:
            by_parent.setdefault(str(item.parent_node_id or '').strip(), []).append(item)
        result: list[NodeRecord] = []
        queue: list[str] = [str(node_id or '').strip()]
        seen: set[str] = set()
        while queue:
            current_id = queue.pop(0)
            if not current_id or current_id in seen:
                continue
            seen.add(current_id)
            current = next((item for item in nodes if item.node_id == current_id), None)
            if current is not None and (include_root or current_id != str(node_id or '').strip()):
                result.append(current)
            queue.extend(item.node_id for item in by_parent.get(current_id, []) if item.node_id not in seen)
        return result

    def _task_node_indexes(self, task_id: str) -> tuple[dict[str, NodeRecord], dict[str, list[str]]]:
        """一次载入任务全部节点，构建 node_id→record 与 parent→children 双索引。

        级联控制（control_nodes 两阶段路径）用它避免每个目标各做一次全量扫描。
        """
        normalized = str(task_id or '').strip()
        nodes = [node for node in self.store.list_nodes(normalized) if str(node.task_id or '').strip() == normalized]
        by_id: dict[str, NodeRecord] = {node.node_id: node for node in nodes}
        by_parent: dict[str, list[str]] = {}
        for node in nodes:
            parent_id = str(node.parent_node_id or '').strip()
            if parent_id:
                by_parent.setdefault(parent_id, []).append(node.node_id)
        return by_id, by_parent

    @staticmethod
    def _subtree_ids(by_parent: dict[str, list[str]], root_id: str) -> list[str]:
        """纯函数 BFS（父先序、含根）。输出顺序即级联施加顺序（fail 必须根先子后）。"""
        result: list[str] = []
        seen: set[str] = set()
        queue: list[str] = [str(root_id or '').strip()]
        while queue:
            current = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            result.append(current)
            queue.extend(child for child in by_parent.get(current, []) if child not in seen)
        return result

    def _require_node_control_target(self, task_id: str, node_id: str) -> tuple[TaskRecord, NodeRecord]:
        task = self.get_task(task_id)
        node = self.get_node(node_id)
        if task is None:
            raise ValueError('task_not_found')
        if node is None or str(node.task_id or '').strip() != str(task.task_id or '').strip():
            raise ValueError('node_not_found')
        return task, node

    async def _apply_pause_node_command(
        self,
        task_id: str,
        *,
        node_ids: list[str],
        cascade: bool = False,
        reason: str = 'manual',
        remark: str = '',
    ) -> dict[str, Any]:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return {'ok': False, 'error': 'task_not_found', 'items': []}
        normalized_reason = str(reason or 'manual').strip().lower()
        if normalized_reason not in {'manual', 'agent', 'error'}:
            raise ValueError('invalid_pause_reason')
        targets: list[str] = []
        seen: set[str] = set()
        for raw_node_id in list(node_ids or []):
            node_id = self.normalize_node_id(str(raw_node_id or '').strip())
            for node in (self._node_subtree(normalized_task_id, node_id) if cascade else [self.get_node(node_id)]):
                if node is None or node.node_id in seen:
                    continue
                seen.add(node.node_id)
                targets.append(node.node_id)
        results: list[dict[str, Any]] = []
        for node_id in targets:
            node = self.get_node(node_id)
            if node is None:
                results.append({'node_id': node_id, 'result': 'not_found'})
                continue
            if str(node.status or '').strip().lower() in {'success', 'failed'}:
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal'})
                continue
            updated = self.log_service.set_node_pause_state(
                normalized_task_id,
                node_id,
                pause_requested=True,
                is_paused=True if normalized_reason == 'error' else bool(node.is_paused),
                pause_reason=normalized_reason,
                remark=remark,
            )
            results.append({'node_id': node_id, 'result': 'paused' if updated is not None else 'not_found'})
        self._land_pause_for_stranded_nodes(normalized_task_id)
        return {'ok': True, 'task_id': normalized_task_id, 'items': results}

    def _land_pause_for_stranded_nodes(self, task_id: str) -> list[str]:
        """把"已请求但永远落不了"的暂停直接挂到节点上。

        暂停原本是纯协作式的：`react_loop._check_pause_or_cancel` 是唯一把
        `pause_requested` 转成 `is_paused` 的地方，而它只在节点真的被跑起来时才执行。
        节点一旦脱离派发面（dispatcher 里没有它的 entry），操作员点的暂停就永久挂着——
        2026-09-23 task:321604599eb0 实测 `pause_requested=True` 挂了 50 分钟没落地。

        只处理"已经有人要求暂停"的节点，且只在看得见派发面的进程里判；仍在运行的节点
        一律不动，保留 `_align_root_node_pause_locked` 的"绝不谎标仍在运行的节点"约束。
        """
        if self.execution_mode not in {'embedded', 'worker'}:
            return []
        normalized_task_id = self.normalize_task_id(task_id)
        dispatcher = self.task_actor_service._dispatchers.get(normalized_task_id)
        live_entries = getattr(dispatcher, '_entries', None) or {}
        landed: list[str] = []
        for node in self.list_nodes(normalized_task_id):
            if not bool(node.pause_requested) or bool(node.is_paused):
                continue
            if str(node.status or '').strip().lower() in {'success', 'failed'}:
                continue
            if node.node_id in live_entries:
                continue
            updated = self.log_service.set_node_pause_state(
                normalized_task_id,
                node.node_id,
                pause_requested=True,
                is_paused=True,
                pause_reason=str(node.pause_reason or '').strip() or 'manual',
            )
            if updated is not None:
                landed.append(node.node_id)
        return landed

    def _resume_entry_settled_conflict(self, node_id: str) -> dict[str, Any] | None:
        """复活"结果已交付父节点"的子节点不会被采纳：父节点已经把它读成失败，
        下一次派开会连同其绑定的验收节点一起按 superseded 清扫掉。返回 None 表示可复活。
        """
        settled_reason = str(self.node_runner.spawn_entry_settlement_reason(node_id) or '').strip()
        if not settled_reason:
            return None
        return {
            'reason': 'entry_settled',
            'detail': settled_reason,
            'hint': '父节点已按失败读到该子节点；请让父节点重派，或先对父节点执行控制动作。',
        }

    async def _apply_resume_node_command(self, task_id: str, *, node_ids: list[str], force: bool = False, schedule_if_inactive: bool = True) -> dict[str, Any]:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return {'ok': False, 'error': 'task_not_found', 'items': []}
        dispatcher = self.task_actor_service._dispatchers.get(normalized_task_id)
        results: list[dict[str, Any]] = []
        for raw_node_id in list(node_ids or []):
            node_id = self.normalize_node_id(str(raw_node_id or '').strip())
            node = self.get_node(node_id)
            if node is None or str(node.task_id or '').strip() != normalized_task_id:
                results.append({'node_id': node_id, 'result': 'not_found'})
                continue
            if str(node.status or '').strip().lower() in {'success', 'failed'}:
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal'})
                continue
            if not force and not (bool(node.is_paused) or bool(node.pause_requested)):
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_not_paused'})
                continue
            if not force:
                # worker 重放（force=True）不复核：leader 否决过的节点不会被入队。
                settled = self._resume_entry_settled_conflict(node_id)
                if settled is not None:
                    results.append({'node_id': node_id, 'result': 'conflict', **settled})
                    continue
            updated = self.log_service.set_node_pause_state(
                normalized_task_id,
                node_id,
                pause_requested=False,
                is_paused=False,
                pause_reason='',
                remark='',
            )
            handshake = normalize_acceptance_handshake(
                ((updated.metadata if updated is not None else node.metadata) or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
            )
            if str(handshake.get('state') or '').strip() == ACCEPTANCE_STATE_WAITING_ACCEPTANCE:
                # 已提交、裁决在途：清暂停位就够了。再起一个执行回合会与正在跑的验收
                # 并行（实盘 task:a0d1bd9bc9fa：15:48 resume 根节点后它和最终验收同时
                # 跑到 15:53；验收通过把任务置 success，迟到的回合随后把根改写成 failed）。
                results.append({'node_id': node_id, 'result': 'deferred', 'reason': 'waiting_acceptance'})
                continue
            if dispatcher is not None and updated is not None:
                # 走 TaskActorService 的复活入口：清标志不等于复活，必须留下活执行器
                # 或把现场落进日志，并由延迟校验兜底（见 resume_node_entry）。
                await self.task_actor_service.resume_node_entry(normalized_task_id, node_id)
            results.append({'node_id': node_id, 'result': 'resumed' if updated is not None else 'not_found'})
        if schedule_if_inactive and dispatcher is None and any(item.get('result') == 'resumed' for item in results) and str(task.status or '').strip().lower() == 'in_progress' and not bool(task.is_paused):
            await self.global_scheduler.enqueue_task(normalized_task_id)
        return {'ok': True, 'task_id': normalized_task_id, 'items': results}

    async def _apply_fail_node_command(self, task_id: str, *, node_ids: list[str], reason: str = '', force: bool = False) -> dict[str, Any]:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return {'ok': False, 'error': 'task_not_found', 'items': []}
        dispatcher = self.task_actor_service._dispatchers.get(normalized_task_id)
        results: list[dict[str, Any]] = []
        for raw_node_id in list(node_ids or []):
            node_id = self.normalize_node_id(str(raw_node_id or '').strip())
            node = self.get_node(node_id)
            if node is None or str(node.task_id or '').strip() != normalized_task_id:
                results.append({'node_id': node_id, 'result': 'not_found'})
                continue
            if str(node.status or '').strip().lower() in {'success', 'failed'} and not (force and dispatcher is not None and node_id in dispatcher._entries):
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal'})
                continue
            if not force and not (bool(node.is_paused) or bool(node.pause_requested)):
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_not_paused'})
                continue
            if dispatcher is not None:
                result = await dispatcher.fail_node(node_id, reason)
            else:
                result = self.node_runner.fail_paused_node(normalized_task_id, node_id, reason)
            results.append({'node_id': node_id, 'result': 'failed', 'status': result.status})
        return {'ok': True, 'task_id': normalized_task_id, 'items': results}

    async def pause_node(
        self,
        task_id: str,
        node_id: str,
        *,
        cascade: bool = False,
        reason: str = 'manual',
        remark: str = '',
    ) -> NodeRecord | None:
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(node_id)
        task, node = self._require_node_control_target(normalized_task_id, normalized_node_id)
        if str(node.status or '').strip().lower() in {'success', 'failed'}:
            raise ValueError('node_terminal')
        normalized_reason = str(reason or 'manual').strip().lower()
        if normalized_reason not in {'manual', 'agent', 'error'}:
            raise ValueError('invalid_pause_reason')
        if self.execution_mode == 'web':
            self._assert_worker_available()
        await self._apply_pause_node_command(
            normalized_task_id,
            node_ids=[normalized_node_id],
            cascade=bool(cascade),
            reason=normalized_reason,
            remark=remark,
        )
        if self.execution_mode == 'web':
            self._enqueue_task_command(
                command_type='pause_node',
                task_id=normalized_task_id,
                session_id=task.session_id,
                payload={'node_ids': [normalized_node_id], 'cascade': bool(cascade), 'reason': normalized_reason, 'remark': str(remark or '')},
            )
        return self.get_node(normalized_node_id)

    async def resume_node(self, task_id: str, node_id: str) -> NodeRecord | None:
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(node_id)
        task, _node = self._require_node_control_target(normalized_task_id, normalized_node_id)
        if self.execution_mode == 'web':
            self._assert_worker_available()
        applied = await self._apply_resume_node_command(normalized_task_id, node_ids=[normalized_node_id], schedule_if_inactive=self.execution_mode != 'web')
        # 否决过的节点不得再入队：worker 侧按 force=True 应用，会绕过同一判据。
        resumed = any(
            str(item.get('result') or '').strip() == 'resumed'
            for item in list(applied.get('items') or [])
            if isinstance(item, dict)
        )
        if self.execution_mode == 'web' and resumed:
            self._enqueue_task_command(
                command_type='resume_node',
                task_id=normalized_task_id,
                session_id=task.session_id,
                payload={'node_ids': [normalized_node_id]},
            )
        return self.get_node(normalized_node_id)

    async def fail_node(self, task_id: str, node_id: str, reason: str = '') -> NodeRecord | None:
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(node_id)
        task, _node = self._require_node_control_target(normalized_task_id, normalized_node_id)
        if self.execution_mode == 'web':
            self._assert_worker_available()
        await self._apply_fail_node_command(normalized_task_id, node_ids=[normalized_node_id], reason=reason)
        if self.execution_mode == 'web':
            self._enqueue_task_command(
                command_type='fail_node',
                task_id=normalized_task_id,
                session_id=task.session_id,
                payload={'node_ids': [normalized_node_id], 'reason': str(reason or '')},
            )
        return self.get_node(normalized_node_id)

    async def control_nodes(
        self,
        task_id: str,
        node_ids: list[str],
        action: str,
        *,
        remark: str = '',
        cascade: bool = False,
        targets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized_task_id = self.normalize_task_id(task_id)
        # 全局形态：只给 task_id + action（不给任何节点）⇒ 作用于整棵任务树。它与「传任务
        # 根节点 + cascade=True」走同一条 _control_nodes_scoped 路径，因此前置校验、错误码、
        # 重叠合并、任务级暂停同步全部一致——不引入第二套「全局」语义。
        normalized_action = str(action or '').strip().lower()
        if not list(node_ids or []) and not list(targets or []) and normalized_action:
            scoped_task = self.get_task(normalized_task_id)
            if scoped_task is None:
                return {'ok': False, 'error': 'task_not_found', 'items': []}
            root_node_id = str(scoped_task.root_node_id or '').strip()
            if not root_node_id:
                return {'ok': False, 'error': 'task_root_missing', 'items': []}
            scoped_result = await self._control_nodes_scoped(
                normalized_task_id,
                [{'index': 0, 'node_id': root_node_id, 'action': normalized_action, 'cascade': True}],
                remark=str(remark or ''),
            )
            return {**scoped_result, 'scope': 'task', 'node_id': root_node_id}
        # targets/级联 → 两阶段原子路径；否则保持既有逐节点语义一字不改。
        normalized_targets = self._normalize_control_targets(targets)
        if normalized_targets or bool(cascade):
            legacy_ids: list[str] = []
            seen_legacy: set[str] = set()
            for raw in list(node_ids or []):
                normalized_id = self.normalize_node_id(str(raw or '').strip())
                if not normalized_id or normalized_id in seen_legacy:
                    continue
                seen_legacy.add(normalized_id)
                legacy_ids.append(normalized_id)
            entries = normalized_targets or [
                {
                    'index': position,
                    'node_id': node_id,
                    'action': str(action or '').strip().lower(),
                    'cascade': True,
                }
                for position, node_id in enumerate(legacy_ids)
            ]
            return await self._control_nodes_scoped(normalized_task_id, entries, remark=str(remark or ''))
        action = str(action or '').strip().lower()
        if action not in {'resume', 'keep_paused', 'fail', 'pause'}:
            raise ValueError('invalid_node_action')
        if action == 'keep_paused' and not str(remark or '').strip():
            raise ValueError('remark_required_for_keep_paused')
        task = self.get_task(normalized_task_id)
        if task is None:
            raise ValueError('task_not_found')
        normalized_ids = [self.normalize_node_id(str(item or '').strip()) for item in list(node_ids or []) if str(item or '').strip()]
        if not normalized_ids:
            raise ValueError('node_ids_required')
        if self.execution_mode == 'web':
            self._assert_worker_available()
        results: list[dict[str, Any]] = []
        valid_ids: list[str] = []
        for node_id in normalized_ids:
            node = self.get_node(node_id)
            if node is None or str(node.task_id or '').strip() != normalized_task_id:
                results.append({'node_id': node_id, 'result': 'not_found'})
                continue
            status = str(node.status or '').strip().lower()
            paused = bool(node.is_paused) or bool(node.pause_requested)
            if status in {'success', 'failed'}:
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal'})
                continue
            if action == 'pause' and paused:
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_already_paused'})
                continue
            if action in {'resume', 'keep_paused', 'fail'} and not paused:
                results.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_not_paused'})
                continue
            if action == 'resume':
                settled = self._resume_entry_settled_conflict(node_id)
                if settled is not None:
                    results.append({'node_id': node_id, 'result': 'conflict', **settled})
                    continue
            valid_ids.append(node_id)
        if action == 'pause':
            applied = await self._apply_pause_node_command(normalized_task_id, node_ids=valid_ids, reason='agent', remark=remark)
        elif action == 'resume':
            applied = await self._apply_resume_node_command(normalized_task_id, node_ids=valid_ids, schedule_if_inactive=self.execution_mode != 'web')
        elif action == 'fail':
            applied = await self._apply_fail_node_command(normalized_task_id, node_ids=valid_ids, reason=remark)
        else:
            for node_id in valid_ids:
                row = self.store.get_task_node_pause(node_id)
                if row is None:
                    continue
                self.log_service.register_node_pause(normalized_task_id, node_id, pause_reason=row.pause_reason, remark=str(remark or ''), delivered=bool(row.delivered))
            applied = {'items': [{'node_id': node_id, 'result': 'kept_paused', 'remark': str(remark or '')} for node_id in valid_ids]}
        results.extend(list(applied.get('items') or []))
        # keep_paused is applied leader-locally and has no worker command type;
        # enqueuing one here previously fell through to fail_node and terminated
        # the node that was meant to stay paused.
        if self.execution_mode == 'web' and valid_ids and action != 'keep_paused':
            command_type = 'pause_node' if action == 'pause' else ('resume_node' if action == 'resume' else 'fail_node')
            self._enqueue_task_command(
                command_type=command_type,
                task_id=normalized_task_id,
                session_id=task.session_id,
                payload={
                    'node_ids': valid_ids,
                    'reason': 'agent' if action == 'pause' else (str(remark or '') if action == 'fail' else ''),
                    'remark': str(remark or ''),
                },
            )
        return {'ok': True, 'task_id': normalized_task_id, 'action': action, 'items': results}

    def _normalize_control_targets(self, targets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """归一化 targets 条目并去重；「同节点不同动作/不同级联范围」的冲突留给重叠检测。"""
        entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str, bool]] = set()
        for item in list(targets or []):
            if not isinstance(item, dict):
                continue
            node_id = self.normalize_node_id(str(item.get('node_id') or '').strip())
            action = str(item.get('action') or '').strip().lower()
            entry_cascade = bool(item.get('cascade', False))
            if not node_id or not action:
                continue
            key = (node_id, action, entry_cascade)
            if key in seen:
                continue
            seen.add(key)
            entries.append({'index': len(entries), 'node_id': node_id, 'action': action, 'cascade': entry_cascade})
        return entries

    async def _control_nodes_scoped(
        self,
        normalized_task_id: str,
        entries: list[dict[str, Any]],
        *,
        remark: str,
    ) -> dict[str, Any]:
        """targets/级联的原子控制路径：先整体校验，任何一项不满足整批打回不生效。

        与旧格式的逐节点冲突语义互补：条目根节点严格校验前置条件（不满足 → 整批
        打回，返回 ok:false 与错误码）；子树内后代的状态冲突逐个跳过并在 items 报告。
        同动作的子树包含关系自动合并（子集条目被吸收，见响应 merged）；跨动作重叠
        才是需要打回的二义性。子树展开是调用时快照，之后新 spawn 的后代不在集内
        （暂停的祖先会延迟其分发）。
        """
        if not entries:
            return {'ok': False, 'error': 'node_ids_required', 'items': []}
        for entry in entries:
            if entry['action'] not in {'resume', 'keep_paused', 'fail', 'pause'}:
                return {'ok': False, 'error': 'invalid_node_action', 'index': entry['index'], 'node_id': entry['node_id'], 'action': entry['action'], 'items': []}
        if any(entry['action'] == 'keep_paused' for entry in entries) and not str(remark or '').strip():
            return {'ok': False, 'error': 'remark_required_for_keep_paused', 'items': []}
        task = self.get_task(normalized_task_id)
        if task is None:
            return {'ok': False, 'error': 'task_not_found', 'items': []}
        if self.execution_mode == 'web':
            self._assert_worker_available()
        # —— 阶段 1：原子校验（不落任何状态）——
        by_id, by_parent = self._task_node_indexes(normalized_task_id)
        missing = [entry['node_id'] for entry in entries if entry['node_id'] not in by_id]
        if missing:
            return {'ok': False, 'error': 'node_not_found', 'node_ids': missing, 'items': []}
        expansions: list[list[str]] = [
            self._subtree_ids(by_parent, entry['node_id']) if entry['cascade'] else [entry['node_id']]
            for entry in entries
        ]
        # 同动作子树重叠自动合并：树结构下任意两棵子树要么不相交、要么一方包含另一方，
        # 相同条目去重后同动作重叠必为真子集——子集条目被覆盖条目吸收。被吸收条目
        # 不再参与根校验与施加，其节点在覆盖条目的结果里逐个报告。
        merged: list[dict[str, Any]] = []
        absorbed: set[int] = set()
        entry_sets = [set(ids) for ids in expansions]
        for position, entry in enumerate(entries):
            for other_position, other in enumerate(entries):
                if other_position == position or other_position in absorbed:
                    continue
                if other['action'] == entry['action'] and entry_sets[position] < entry_sets[other_position]:
                    absorbed.add(position)
                    merged.append({
                        'index': entry['index'],
                        'node_id': entry['node_id'],
                        'action': entry['action'],
                        'into_index': other['index'],
                        'into_node_id': other['node_id'],
                    })
                    break
        kept_entries = [entry for position, entry in enumerate(entries) if position not in absorbed]
        kept_expansions = [ids for position, ids in enumerate(expansions) if position not in absorbed]
        # 跨动作重叠仍然原子打回：同一节点被两个不同动作声明是无法消解的二义性。
        owners: dict[str, list[dict[str, Any]]] = {}
        for entry, ids in zip(kept_entries, kept_expansions):
            owner = {'index': entry['index'], 'node_id': entry['node_id'], 'action': entry['action'], 'cascade': entry['cascade']}
            for node_id in ids:
                owners.setdefault(node_id, []).append(owner)
        conflicts = [
            {'node_id': node_id, 'covered_by': owner_list}
            for node_id, owner_list in owners.items()
            if len(owner_list) > 1
        ]
        if conflicts:
            conflicts.sort(key=lambda item: item['node_id'])
            return {'ok': False, 'error': 'subtree_overlap', 'conflicts': conflicts, 'items': []}
        for entry in kept_entries:
            node = by_id[entry['node_id']]
            status = str(node.status or '').strip().lower()
            paused = bool(node.is_paused) or bool(node.pause_requested)
            if status in {'success', 'failed'}:
                return {'ok': False, 'error': 'node_terminal', 'index': entry['index'], 'node_id': entry['node_id'], 'items': []}
            if entry['action'] == 'pause' and paused:
                # 级联 pause 容忍已暂停的根（跳过根继续级联后代）：这是「先级联暂停
                # 再级联失败」两步配方的入口——根因错误/人工已暂停而后代仍在跑是常态。
                if not entry['cascade']:
                    return {'ok': False, 'error': 'node_already_paused', 'index': entry['index'], 'node_id': entry['node_id'], 'items': []}
            elif entry['action'] in {'resume', 'keep_paused', 'fail'} and not paused:
                return {'ok': False, 'error': 'node_not_paused', 'index': entry['index'], 'node_id': entry['node_id'], 'items': []}
            if entry['action'] == 'resume' and not entry['cascade']:
                # 单目标复活：结清判据按原子打回处理（与 node_not_paused 同级）。级联条目
                # 留给阶段 2 逐节点跳过，避免一个已结清的后代挡住整棵子树。
                settled = self._resume_entry_settled_conflict(entry['node_id'])
                if settled is not None:
                    return {
                        'ok': False,
                        'error': 'entry_settled',
                        'index': entry['index'],
                        'node_id': entry['node_id'],
                        'items': [],
                        **settled,
                    }
        for entry, ids in zip(kept_entries, kept_expansions):
            if entry['action'] != 'fail' or not entry['cascade']:
                continue
            # 级联 fail 护栏：子树内所有非终态后代必须已暂停，否则整批打回。
            # 判据与旧路径 fail 门同源（is_paused or pause_requested）。
            blocking: list[str] = []
            for node_id in ids:
                if node_id == entry['node_id']:
                    continue
                node = by_id.get(node_id)
                if node is None or str(node.status or '').strip().lower() in {'success', 'failed'}:
                    continue
                if not (bool(node.is_paused) or bool(node.pause_requested)):
                    blocking.append(node_id)
            if blocking:
                return {
                    'ok': False,
                    'error': 'subtree_not_fully_paused',
                    'index': entry['index'],
                    'node_id': entry['node_id'],
                    'blocking_node_ids': blocking,
                    'items': [],
                }
        # —— 阶段 2：施加（校验全过才会执行到这里）——
        dispatcher = self.task_actor_service._dispatchers.get(normalized_task_id)
        root_node_id = str(task.root_node_id or '').strip()
        items: list[dict[str, Any]] = []
        target_summaries: list[dict[str, Any]] = []
        enqueue_requests: list[dict[str, Any]] = []
        global_scope_actions: list[str] = []
        any_resumed = False
        for entry, ids in zip(kept_entries, kept_expansions):
            action = entry['action']
            applied_ids: list[str] = []
            skipped = 0
            if action == 'pause':
                for node_id in ids:
                    node = self.get_node(node_id)
                    if node is None or str(node.task_id or '').strip() != normalized_task_id:
                        items.append({'node_id': node_id, 'result': 'not_found', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if str(node.status or '').strip().lower() in {'success', 'failed'}:
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if bool(node.is_paused) or bool(node.pause_requested):
                        # 已暂停后代跳过不覆写：保留 pause_reason=error 等原登记与心跳重试计数。
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_already_paused', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    updated = self.log_service.set_node_pause_state(
                        normalized_task_id,
                        node_id,
                        pause_requested=True,
                        is_paused=bool(node.is_paused),
                        pause_reason='agent',
                        remark=remark,
                    )
                    if updated is None:
                        items.append({'node_id': node_id, 'result': 'not_found', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    items.append({'node_id': node_id, 'result': 'paused', 'target_index': entry['index']})
                    applied_ids.append(node_id)
            elif action == 'resume':
                # 两遍式：先全清旗再续跑，避免被唤醒的父协程命中尚未清旗的后代重新暂停。
                resume_candidates: list[str] = []
                for node_id in ids:
                    node = self.get_node(node_id)
                    if node is None or str(node.task_id or '').strip() != normalized_task_id:
                        items.append({'node_id': node_id, 'result': 'not_found', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if str(node.status or '').strip().lower() in {'success', 'failed'}:
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if not (bool(node.is_paused) or bool(node.pause_requested)):
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_not_paused', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    settled = self._resume_entry_settled_conflict(node_id)
                    if settled is not None:
                        items.append({'node_id': node_id, 'result': 'conflict', 'target_index': entry['index'], **settled})
                        skipped += 1
                        continue
                    updated = self.log_service.set_node_pause_state(
                        normalized_task_id,
                        node_id,
                        pause_requested=False,
                        is_paused=False,
                        pause_reason='',
                        remark='',
                    )
                    if updated is None:
                        items.append({'node_id': node_id, 'result': 'not_found', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    resume_candidates.append(node_id)
                    items.append({'node_id': node_id, 'result': 'resumed', 'target_index': entry['index']})
                    applied_ids.append(node_id)
                if dispatcher is not None:
                    for node_id in resume_candidates:
                        await self.task_actor_service.resume_node_entry(normalized_task_id, node_id)
                any_resumed = any_resumed or bool(resume_candidates)
            elif action == 'fail':
                # 根先、后代 BFS 随后：失败结果先占据根 future；后代 fail 唤醒的父协程
                # 已终态，残余写由运行时终态短路兜住（顺序反过来会撕裂任务状态）。
                for node_id in ids:
                    node = self.get_node(node_id)  # 重读记录：后代可能已被终态短路关闭。
                    if node is None or str(node.task_id or '').strip() != normalized_task_id:
                        items.append({'node_id': node_id, 'result': 'not_found', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if str(node.status or '').strip().lower() in {'success', 'failed'}:
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if not (bool(node.is_paused) or bool(node.pause_requested)):
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_not_paused', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if dispatcher is not None:
                        result = await dispatcher.fail_node(node_id, remark)
                    else:
                        result = self.node_runner.fail_paused_node(normalized_task_id, node_id, remark)
                    items.append({'node_id': node_id, 'result': 'failed', 'status': result.status, 'target_index': entry['index']})
                    applied_ids.append(node_id)
            else:  # keep_paused：leader 本地操作，不产生 worker 命令（沿用旧契约）。
                for node_id in ids:
                    node = self.get_node(node_id)
                    if node is None or str(node.task_id or '').strip() != normalized_task_id:
                        items.append({'node_id': node_id, 'result': 'not_found', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if str(node.status or '').strip().lower() in {'success', 'failed'}:
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_terminal', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    if not (bool(node.is_paused) or bool(node.pause_requested)):
                        items.append({'node_id': node_id, 'result': 'conflict', 'reason': 'node_not_paused', 'target_index': entry['index']})
                        skipped += 1
                        continue
                    row = self.store.get_task_node_pause(node_id)
                    if row is not None:
                        self.log_service.register_node_pause(
                            normalized_task_id,
                            node_id,
                            pause_reason=row.pause_reason,
                            remark=str(remark or ''),
                            delivered=bool(row.delivered),
                        )
                    items.append({'node_id': node_id, 'result': 'kept_paused', 'remark': str(remark or ''), 'target_index': entry['index']})
                    applied_ids.append(node_id)
            target_summaries.append({
                'index': entry['index'],
                'node_id': entry['node_id'],
                'action': action,
                'cascade': entry['cascade'],
                'applied': len(applied_ids),
                'skipped': skipped,
            })
            if action != 'keep_paused' and applied_ids:
                enqueue_requests.append({'entry': entry, 'node_ids': list(applied_ids)})
                if action in {'pause', 'resume'} and root_node_id and root_node_id in ids:
                    global_scope_actions.append(action)
        # 全局作用域（动作覆盖任务根节点）⇒ 复用 pause_task/resume_task 的任务级运行时
        # 副作用：「根节点 + cascade ≡ 全局」不只是状态一致，取消调度排队、唤醒排队等待、
        # 分发失败态复位也走同一条实现，避免两套「全局」语义漂移。
        for global_action in dict.fromkeys(global_scope_actions):
            if global_action == 'pause':
                await self.pause_task(normalized_task_id)
            else:
                await self.resume_task(normalized_task_id)
        if any_resumed:
            # 沿用 _apply_resume_node_command 的调度恢复语义。重读任务而不是用调用开始时的
            # 快照：恢复刚把暂停标志清掉，旧快照会误判「仍在暂停」而跳过重新入队。
            resumed_task = self.get_task(normalized_task_id)
            if (
                self.execution_mode != 'web'
                and dispatcher is None
                and str(getattr(resumed_task, 'status', '') or '').strip().lower() == 'in_progress'
                and not bool(getattr(resumed_task, 'is_paused', False))
            ):
                await self.global_scheduler.enqueue_task(normalized_task_id)
        if self.execution_mode == 'web':
            # 每条目一条命令：携带 leader 已展开的显式 node_ids + cascade=False，
            # worker 不再重展开（防两次展开漂移），并保条目顺序与 remark 保真。
            for request in enqueue_requests:
                entry = request['entry']
                entry_action = entry['action']
                command_type = 'pause_node' if entry_action == 'pause' else ('resume_node' if entry_action == 'resume' else 'fail_node')
                self._enqueue_task_command(
                    command_type=command_type,
                    task_id=normalized_task_id,
                    session_id=task.session_id,
                    payload={
                        'node_ids': request['node_ids'],
                        'cascade': False,
                        'reason': 'agent' if entry_action == 'pause' else (str(remark or '') if entry_action == 'fail' else ''),
                        'remark': str(remark or ''),
                    },
                )
        return {'ok': True, 'task_id': normalized_task_id, 'items': items, 'targets': target_summaries, 'merged': merged}

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            self.task_actor_service.request_cancel(task_id)
            await self.global_scheduler.cancel_task(task_id)
            current = self.get_task(task_id)
            if current is not None and current.status == 'in_progress':
                # 暂停中的任务也必须能取消。以前这里带 `not is_paused` 守卫，于是"暂停 → 取消"
                # 只留下一个 cancel_requested：没有任何协程会去观察它，任务永久挂在
                # in_progress + paused（实测 4 个残任务只能绕 worker 命令队列结算）。
                updated = self.log_service.mark_task_failed(task_id, reason='canceled')
                if bool(getattr(updated, 'is_paused', False)) or bool(getattr(updated, 'pause_requested', False)):
                    # 终态任务挂着"已暂停"会让卡片显示成还能恢复的样子。
                    self.log_service.set_pause_state(task_id, pause_requested=False, is_paused=False)
                # 残留在 in_progress 的节点不等下一次 worker 启动才收尸：立即扫，
                # 正文指针保留进 failure_reason。
                self.log_service.sweep_residual_nodes(task_id)
        else:
            self._assert_worker_available()
            self.log_service.request_cancel(task_id)
            task = self.get_task(task_id)
            if task is not None:
                self._enqueue_task_command(
                    command_type='cancel_task',
                    task_id=task.task_id,
                    session_id=task.session_id,
                    payload={'task_id': task.task_id},
                )
        return self.get_task(task_id)

    async def pause_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            self.task_actor_service.request_pause(task_id)
            await self.global_scheduler.cancel_task(task_id)
        else:
            self._assert_worker_available()
            self.log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            task = self.get_task(task_id)
            if task is not None:
                self._enqueue_task_command(
                    command_type='pause_task',
                    task_id=task.task_id,
                    session_id=task.session_id,
                    payload={'task_id': task.task_id},
                )
        # 磁盘治理（P1）防死锁：暂停生效后唤醒该任务在预算队列中排队的工具调用。
        self._abort_queued_waits(task_id)
        self._land_pause_for_stranded_nodes(task_id)
        return self.get_task(task_id)

    async def force_pause_task_durably(self, task_id: str) -> TaskRecord | None:
        """Pause a task durably even when the worker is offline or starting.

        Used by graceful-shutdown paths: the pause must be persisted no matter
        what the worker state is, so the next startup can resume it silently
        instead of reporting an abnormal stop. When the ordinary pause
        requires a live worker, the durable flags are written directly and the
        worker command is still enqueued best-effort so a running actor stops
        at its next safe boundary.
        """
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            return await self.pause_task(task_id)
        self.log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
        task = self.get_task(task_id)
        if task is not None:
            try:
                self._enqueue_task_command(
                    command_type='pause_task',
                    task_id=task.task_id,
                    session_id=task.session_id,
                    payload={'task_id': task.task_id},
                )
            except Exception:
                logger.debug("worker pause command enqueue skipped for {}", task_id)
        return self.get_task(task_id)

    def release_task_worker_lease_durably(self) -> bool:
        """Delete the task-worker lease row before the worker process is
        stopped, so a fast restart (within the lease TTL) does not collide
        with the stale lease and refuse to start. Only the web process that
        owns the managed worker calls this during graceful shutdown."""
        row = self.store.get_worker_lease(_WORKER_LEASE_ROLE)
        if row is None:
            return False
        worker_id = str(row.get('worker_id') or '').strip()
        if not worker_id:
            return False
        self.store.release_worker_lease(role=_WORKER_LEASE_ROLE, worker_id=worker_id)
        logger.info("task worker lease released during graceful shutdown (worker_id={})", worker_id)
        return True

    def _task_pause_drain_active(self, task_id: str, task: TaskRecord | None = None) -> bool:
        """任务是否处于「暂停排空」中：暂停标志已置位，但 worker 尚未消费
        完 pause_task 命令，actor 可能仍在运行并写任务文件。

        worker 离线/失效时永不排空——持久暂停标志即权威。前端暂停提示
        （draining）与删除前等待共用该判定，两处条件必须同源。
        """
        if self.execution_mode != 'web':
            return False
        record = task if task is not None else self.get_task(task_id)
        if record is None:
            return False
        status = str(getattr(record, 'status', '') or '').strip().lower()
        if status != 'in_progress':
            return False
        if not (bool(getattr(record, 'is_paused', False)) or bool(getattr(record, 'pause_requested', False))):
            return False
        if self.worker_state() not in {_WORKER_STATE_ONLINE, _WORKER_STATE_STARTING}:
            return False
        try:
            unfinished = [
                item
                for item in self.store.list_unfinished_task_commands(command_type='pause_task')
                if str(item.get('task_id') or '').strip() == task_id
            ]
        except Exception:
            unfinished = []
        return bool(unfinished)

    async def _await_task_pause_drain(
        self,
        task_id: str,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
    ) -> bool:
        """轮询等待暂停排空结束；到期仍在排空则返回 False。"""
        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        while self._task_pause_drain_active(task_id):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(max(0.05, float(poll_interval_seconds)))
        return True

    def get_task_pause_state_payload(self, task_id: str) -> dict[str, Any] | None:
        """Pause-drain visibility for the task-card synchronous pause hint.

        `draining` is true only while this task's `pause_task` command is
        still unfinished AND a live worker (online/starting) can process it —
        i.e. actors may still be running even though the durable paused flags
        are already set. A stale/offline worker never drains, so the durable
        flags are the truth there. `internal_total` counts non-terminal
        runtime frames (active ∪ runnable ∪ waiting); frame rows persist for
        resume, so `internal_stopped` mirrors the total once draining ends.
        """
        task_id = self.normalize_task_id(task_id)
        task = self.get_task(task_id)
        if task is None:
            return None
        runtime_state = self.log_service.read_runtime_state(task_id) or {}
        internal_ids: set[str] = set()
        for key in ('active_node_ids', 'runnable_node_ids', 'waiting_node_ids'):
            for item in list(runtime_state.get(key) or []):
                node_id = str(item or '').strip()
                if node_id:
                    internal_ids.add(node_id)
        internal_total = len(internal_ids)
        worker_state = self.worker_state()
        draining = self._task_pause_drain_active(task_id, task=task)
        return {
            'task_id': task_id,
            'status': str(task.status or ''),
            'is_paused': bool(task.is_paused),
            'pause_requested': bool(task.pause_requested),
            'draining': draining,
            'internal_total': internal_total,
            'internal_stopped': 0 if draining else internal_total,
            'worker_state': str(worker_state or ''),
        }

    async def resume_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            task = self.get_task(task_id)
            if task is None:
                return None
            self.task_actor_service.clear_pause(task_id)
            await self.global_scheduler.enqueue_task(task_id)
        else:
            self._assert_worker_available()
            task = self.get_task(task_id)
            if task is None:
                return None
            self.log_service.set_pause_state(task_id, pause_requested=False, is_paused=False)
            self._enqueue_task_command(
                command_type='resume_task',
                task_id=task.task_id,
                session_id=task.session_id,
                payload={'task_id': task.task_id},
            )
        return self.get_task(task_id)


    @staticmethod
    def _runtime_session_context(session_id: str) -> dict[str, str]:
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        channel, _, chat_id = normalized_session_id.partition(':')
        normalized_channel = str(channel or 'web').strip() or 'web'
        normalized_chat_id = str(chat_id or normalized_session_id).strip() or normalized_session_id
        return {
            'session_key': normalized_session_id,
            'channel': normalized_channel,
            'chat_id': normalized_chat_id,
        }

    async def delete_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        task = self.get_task(task_id)
        if task is None:
            return None
        if not (bool(task.is_paused) or task.status in {'success', 'failed'}):
            raise ValueError('task_not_paused')
        if self.global_scheduler.is_active(task_id):
            try:
                await asyncio.wait_for(self.global_scheduler.wait(task_id), timeout=2.0)
            except asyncio.TimeoutError as exc:
                raise ValueError('task_still_stopping') from exc
        if self.global_scheduler.is_active(task_id) or self.global_scheduler.is_queued(task_id):
            raise ValueError('task_still_stopping')
        # web 模式暂停排空：暂停标志置位不代表 worker actor 已停止，必须等
        # pause_task 命令被消费完再删文件，否则边写边删产生半写残留；到期
        # 未排空统一按「仍在停止」拒绝，调用方可在暂停生效后重试。
        if not await self._await_task_pause_drain(task_id, timeout_seconds=_DELETE_PAUSE_DRAIN_TIMEOUT_SECONDS):
            raise ValueError('task_still_stopping')
        await self._wipe_task_data(task, reason='user_delete')
        return task

    async def bulk_delete_tasks(self, task_ids: list[str] | None) -> dict[str, Any]:
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        items: list[dict[str, Any]] = []
        deleted_count = 0
        failed_count = 0
        for task_id in normalized_task_ids:
            try:
                deleted = await self.delete_task(task_id)
            except ValueError as exc:
                items.append({
                    'task_id': task_id,
                    'result': 'failed',
                    'error': str(exc or 'delete_failed').strip() or 'delete_failed',
                })
                failed_count += 1
                continue
            if deleted is None:
                items.append({
                    'task_id': task_id,
                    'result': 'not_found',
                    'error': 'task_not_found',
                })
                failed_count += 1
                continue
            items.append({
                'task_id': task_id,
                'result': 'deleted',
            })
            deleted_count += 1
        return {
            'items': items,
            'deleted_count': deleted_count,
            'failed_count': failed_count,
        }

    async def clear_task_temp_files(self, task_id: str) -> dict[str, Any] | None:
        """只清任务临时目录（temp/tasks/<id>），保留任务行、节点、产出与 event-history。

        与 `delete_task` 的区别是本方法不删任何数据库记录，因此任务卡片与历史
        回放照常。目录路径按删除链路的 S0 口径取双路径（runtime_meta 记录的实际
        路径 + 确定性默认路径），防止 meta 指向别处时清错或漏清。

        仅终态任务可清：任务目录是 exec / filesystem 工具的默认落点，
        in_progress 与 paused（可能被 resume）的回合随时会再写进去。
        """
        task_id = self.normalize_task_id(task_id)
        task = self.get_task(task_id)
        if task is None:
            return None
        if str(task.status or '').strip().lower() not in {'success', 'failed'}:
            raise ValueError('task_not_terminal')
        return await asyncio.to_thread(self._clear_task_temp_dirs, task_id)

    def _clear_task_temp_dirs(self, task_id: str) -> dict[str, Any]:
        candidates = list(dict.fromkeys((
            self._effective_task_temp_dir(task_id),
            self._task_temp_dir(task_id, create=False),
        )))
        # 绝不允许作用到 temp 根或 temp 本身：meta 缺失/兜底值曾把这两级带进来。
        protected = {
            self._task_temp_root(create=False).resolve(strict=False),
            self._temp_root().resolve(strict=False),
            self._workspace_root().resolve(strict=False),
        }
        removed: list[str] = []
        failed: list[str] = []
        freed_bytes = 0
        for path in candidates:
            resolved = path.resolve(strict=False)
            if resolved in protected:
                continue
            if not path.exists():
                continue
            size = self._directory_size_bytes(path)
            if remove_tree(path):
                removed.append(str(path))
                freed_bytes += size
            else:
                failed.append(str(path))
        try:
            self._reconcile_task_disk_usage(task_id)
        except Exception:
            logger.warning('task temp clear: disk usage reconcile failed for {}', task_id, exc_info=True)
        logger.info(
            'task temp clear: {} removed={} failed={} freed_bytes={}',
            task_id, len(removed), len(failed), freed_bytes,
        )
        return {
            'found': True,
            'task_id': task_id,
            'removed_dirs': removed,
            'failed_dirs': failed,
            'freed_bytes': int(freed_bytes),
        }

    async def _delete_task_with_task_delete_semantics(self, task_id: str) -> TaskRecord | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress' and not bool(getattr(task, 'is_paused', False)):
            await self.pause_task(task_id)
        return await self.delete_task(task_id)

    async def wait_for_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.global_scheduler.wait(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        return self.store.get_task(task_id)

    def task_stats(
        self,
        *,
        mode: str,
        task_keywords: list[str] | None = None,
        date_from: str = '',
        date_to: str = '',
        task_ids: list[str] | None = None,
        sort: str = 'time',
    ) -> dict[str, Any]:
        normalized_mode = str(mode or '').strip().lower()
        if normalized_mode not in {'list', 'id'}:
            raise ValueError('invalid_mode')
        normalized_sort = str(sort or '').strip().lower()
        if normalized_sort not in {'time', 'size'}:
            normalized_sort = 'time'
        if normalized_mode == 'list':
            start_at = self._parse_task_stats_date(date_from, field_name='from')
            end_at = self._parse_task_stats_date(date_to, field_name='to') + timedelta(days=1) - timedelta(milliseconds=1)
            if end_at < start_at:
                raise ValueError('invalid_date_range')
            keywords = self._normalize_task_keyword_list(task_keywords)
            try:
                disk_usages = self.store.get_task_disk_usages(None) or {}
            except Exception:
                disk_usages = {}
            matched: list[tuple[float, dict[str, Any]]] = []
            for task in list(self.store.list_tasks()):
                created_at = self._parse_task_timestamp(str(getattr(task, 'created_at', '') or ''))
                if created_at is None or created_at < start_at or created_at > end_at:
                    continue
                prompt = str(getattr(task, 'user_request', '') or '')
                if keywords and not any(keyword.casefold() in prompt.casefold() for keyword in keywords):
                    continue
                task_id = str(getattr(task, 'task_id', '') or '').strip()
                usage = int(disk_usages.get(task_id) or 0) if task_id else 0
                matched.append((created_at.timestamp(), self._task_stats_item(task, disk_usage_bytes=usage or None)))
            if normalized_sort == 'size':
                matched.sort(key=lambda item: (int(item[1].get('disk_usage_bytes') or 0), item[0]), reverse=True)
            else:
                matched.sort(key=lambda item: item[0], reverse=True)
            return {
                'mode': 'list',
                'from': str(date_from or '').strip(),
                'to': str(date_to or '').strip(),
                'sort': normalized_sort,
                'items': [item for _, item in matched],
            }
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        items: list[dict[str, Any]] = []
        for task_id in normalized_task_ids:
            task = self.get_task(task_id)
            if task is None:
                items.append(self._task_not_found_item(task_id))
                continue
            item = self._task_stats_item(task)
            item['result'] = 'ok'
            items.append(item)
        return {
            'mode': 'id',
            'items': items,
        }

    def task_delete_preview(self, *, task_ids: list[str] | None, session_id: str = 'web:shared') -> dict[str, Any]:
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        items: list[dict[str, Any]] = []
        for task_id in normalized_task_ids:
            task = self.get_task(task_id)
            if task is None:
                items.append(self._task_not_found_item(task_id))
                continue
            items.append(self._task_stats_item(task))
        self._prune_pending_task_delete_confirmations()
        confirmation_token = new_command_id()
        self._pending_task_delete_confirmations[confirmation_token] = {
            'task_ids': list(normalized_task_ids),
            'session_id': normalized_session_id,
            'created_mono': time.monotonic(),
            'preview_items': list(items),
        }
        return {
            'mode': 'preview',
            'confirmation_token': confirmation_token,
            'items': items,
        }

    async def task_delete_confirm(
        self,
        *,
        task_ids: list[str] | None,
        confirmation_token: str,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        normalized_token = str(confirmation_token or '').strip()
        if not normalized_token:
            raise ValueError('confirmation_token_required')
        self._prune_pending_task_delete_confirmations()
        pending = self._pending_task_delete_confirmations.pop(normalized_token, None)
        if pending is None:
            return {
                'mode': 'confirm',
                'items': [self._task_token_mismatch_item(task_id) for task_id in normalized_task_ids],
            }
        if list(pending.get('task_ids') or []) != normalized_task_ids or str(pending.get('session_id') or '') != normalized_session_id:
            return {
                'mode': 'confirm',
                'items': [self._task_token_mismatch_item(task_id) for task_id in normalized_task_ids],
            }
        items: list[dict[str, Any]] = []
        for task_id in normalized_task_ids:
            items.append(await self._delete_task_with_confirmation(task_id))
        return {
            'mode': 'confirm',
            'items': items,
        }

    async def _delete_task_with_confirmation(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            return self._task_not_found_item(task_id)
        snapshot = self._task_stats_item(task)
        try:
            deleted = await self._delete_task_with_task_delete_semantics(task_id)
        except ValueError as exc:
            if str(exc) in {'task_not_paused', 'task_still_stopping'}:
                snapshot['result'] = 'still_stopping'
                return snapshot
            raise
        if deleted is None:
            return self._task_not_found_item(task_id)
        snapshot['result'] = 'deleted'
        return snapshot

    def _prune_pending_task_delete_confirmations(self) -> None:
        now_mono = time.monotonic()
        expired = [
            token
            for token, entry in list(self._pending_task_delete_confirmations.items())
            if (now_mono - float(entry.get('created_mono') or 0.0)) > _TASK_DELETE_CONFIRM_TTL_SECONDS
        ]
        for token in expired:
            self._pending_task_delete_confirmations.pop(token, None)

    @staticmethod
    def _normalize_task_keyword_list(raw_keywords: list[str] | None) -> list[str]:
        if raw_keywords in (None, ''):
            return []
        if not isinstance(raw_keywords, list):
            raise ValueError('任务关键词 must be an array')
        return [str(item or '').strip() for item in list(raw_keywords or []) if str(item or '').strip()]

    def _normalize_task_id_list(self, raw_task_ids: list[str] | None, *, allow_empty: bool) -> list[str]:
        if not isinstance(raw_task_ids, list):
            raise ValueError('任务id列表 must be an array')
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_task_id in list(raw_task_ids or []):
            task_id = self.normalize_task_id(str(raw_task_id or '').strip())
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            normalized.append(task_id)
        if not allow_empty and not normalized:
            raise ValueError('任务id列表 must not be empty')
        return normalized

    @staticmethod
    def _parse_task_stats_date(value: str, *, field_name: str) -> datetime:
        text = str(value or '').strip()
        parts = text.split('/')
        if len(parts) != 3:
            raise ValueError(f'{field_name}_invalid')
        try:
            year, month, day = (int(part) for part in parts)
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            return datetime(year, month, day, tzinfo=local_tz)
        except ValueError as exc:
            raise ValueError(f'{field_name}_invalid') from exc

    @staticmethod
    def _parse_task_timestamp(value: str) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(local_tz)

    @staticmethod
    def _prompt_preview_100(value: Any) -> str:
        text = ' '.join(str(value or '').split())
        return text[:100]

    def _task_stats_item(self, task: TaskRecord, *, disk_usage_bytes: int | None = None) -> dict[str, Any]:
        """disk_usage_bytes 为 None/0 时回退目录实测（新任务尚无记账；
        list 模式由调用方批量传入记账表值，避免逐任务 rglob）。"""
        if disk_usage_bytes is None or int(disk_usage_bytes or 0) <= 0:
            disk_usage_bytes = self._task_disk_usage_bytes(task.task_id)
        return {
            'task_id': task.task_id,
            'created_at': str(getattr(task, 'created_at', '') or ''),
            'status': str(getattr(task, 'status', '') or ''),
            'prompt_preview_100': self._prompt_preview_100(getattr(task, 'user_request', '')),
            'disk_usage_bytes': int(disk_usage_bytes or 0),
        }

    def _task_not_found_item(self, task_id: str) -> dict[str, Any]:
        return {
            'task_id': str(task_id or '').strip(),
            'created_at': '',
            'status': 'not_found',
            'prompt_preview_100': '',
            'disk_usage_bytes': 0,
            'result': 'not_found',
        }

    def _task_token_mismatch_item(self, task_id: str) -> dict[str, Any]:
        return {
            'task_id': str(task_id or '').strip(),
            'result': 'token_mismatch',
        }

    def _task_disk_usage_bytes(self, task_id: str) -> int:
        normalized_task_id = self.normalize_task_id(task_id)
        return sum(
            self._directory_size_bytes(path)
            for path in (
                self._task_file_dir_path(normalized_task_id),
                self._task_artifact_dir(normalized_task_id),
                self._task_event_history_dir(normalized_task_id),
                self._effective_task_temp_dir(normalized_task_id),
            )
        )

    @staticmethod
    def _directory_size_bytes(path: Path) -> int:
        target = Path(path)
        if not target.exists():
            return 0
        if target.is_file():
            try:
                return int(target.stat().st_size)
            except OSError:
                return 0
        total = 0
        for child in target.rglob('*'):
            try:
                if child.is_file():
                    total += int(child.stat().st_size)
            except OSError:
                continue
        return total

    def latest_worker_status(self) -> dict[str, Any] | None:
        items = self.store.list_worker_status(role='task_worker')
        return items[0] if items else None

    @staticmethod
    def _parse_worker_timestamp(value: Any) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _managed_worker_runtime_snapshot(self) -> dict[str, object]:
        if self.execution_mode != 'web':
            return {}
        try:
            snapshot = managed_worker_snapshot(starting_grace_s=_WORKER_STATUS_STARTING_GRACE_SECONDS)
        except Exception:
            return {}
        return dict(snapshot or {}) if isinstance(snapshot, dict) else {}

    def _worker_status_stale_after_seconds(
        self,
        item: dict[str, Any] | None,
        *,
        override_seconds: float | None = None,
    ) -> float:
        if override_seconds is not None:
            return max(0.0, float(override_seconds))
        payload = item.get('payload') if isinstance(item, dict) else {}
        raw_active_task_count = payload.get('active_task_count') if isinstance(payload, dict) else 0
        try:
            active_task_count = max(0, int(raw_active_task_count or 0))
        except (TypeError, ValueError):
            active_task_count = 0
        if active_task_count > 0:
            return _WORKER_STATUS_ACTIVE_TASK_STALE_AFTER_SECONDS
        return _WORKER_STATUS_STALE_AFTER_SECONDS

    def worker_status_stale_after_seconds(
        self,
        *,
        item: dict[str, Any] | None = None,
        override_seconds: float | None = None,
    ) -> float:
        return float(self._worker_status_stale_after_seconds(item, override_seconds=override_seconds))

    def _worker_online_from_item(
        self,
        item: dict[str, Any] | None,
        *,
        stale_after_seconds: float | None = None,
    ) -> bool:
        if not item:
            return self.execution_mode in {'embedded', 'worker'}
        status = str(item.get('status') or item.get('state') or '').strip().lower()
        if status in _WORKER_STATUS_TERMINAL_STATES:
            return False
        if self.execution_mode in {'embedded', 'worker'}:
            return True
        updated_dt = self._parse_worker_timestamp(item.get('updated_at'))
        if updated_dt is None:
            return False
        age_seconds = max(0.0, (datetime.now(timezone.utc) - updated_dt).total_seconds())
        stale_window_seconds = self._worker_status_stale_after_seconds(item, override_seconds=stale_after_seconds)
        return age_seconds <= stale_window_seconds

    def _managed_worker_is_starting(
        self,
        *,
        item: dict[str, Any] | None,
        snapshot: dict[str, object] | None = None,
    ) -> bool:
        current = item if isinstance(item, dict) else {}
        worker_snapshot = dict(snapshot or {}) if isinstance(snapshot, dict) else self._managed_worker_runtime_snapshot()
        if not worker_snapshot:
            return False
        if not bool(worker_snapshot.get('active')) or not bool(worker_snapshot.get('auto_worker_enabled')):
            return False
        if not bool(worker_snapshot.get('starting')):
            return False
        started_at_dt = self._parse_worker_timestamp(worker_snapshot.get('started_at'))
        if not current:
            return True
        current_updated_at = self._parse_worker_timestamp(current.get('updated_at'))
        if started_at_dt is None or current_updated_at is None:
            return True
        return current_updated_at < started_at_dt

    def worker_state(
        self,
        *,
        item: dict[str, Any] | None = None,
        stale_after_seconds: float | None = None,
    ) -> str:
        current = dict(item or self.latest_worker_status() or {})
        if self._managed_worker_is_starting(item=current if current else None):
            return _WORKER_STATE_STARTING
        if not current:
            return _WORKER_STATE_ONLINE if self.execution_mode in {'embedded', 'worker'} else _WORKER_STATE_OFFLINE
        status = str(current.get('status') or current.get('state') or '').strip().lower()
        if status in _WORKER_STATUS_TERMINAL_STATES:
            return _WORKER_STATE_STOPPED
        if self._worker_online_from_item(current, stale_after_seconds=stale_after_seconds):
            return _WORKER_STATE_ONLINE
        return _WORKER_STATE_STALE

    def is_worker_online(self, *, stale_after_seconds: float | None = None) -> bool:
        return self.worker_state(stale_after_seconds=stale_after_seconds) == _WORKER_STATE_ONLINE

    def worker_status_payload(
        self,
        *,
        item: dict[str, Any] | None = None,
        stale_after_seconds: float | None = None,
    ) -> dict[str, object]:
        current = dict(item or self.latest_worker_status() or {})
        window_seconds = self.worker_status_stale_after_seconds(
            item=current if current else None,
            override_seconds=stale_after_seconds,
        )
        state = self.worker_state(
            item=current if current else None,
            stale_after_seconds=window_seconds,
        )
        last_seen_at = str(current.get('updated_at') or '').strip()
        return {
            'worker': current or None,
            'worker_online': state == _WORKER_STATE_ONLINE,
            'worker_state': state,
            'worker_last_seen_at': last_seen_at,
            'worker_control_available': state == _WORKER_STATE_ONLINE,
            'worker_stale_after_seconds': window_seconds,
            **self._tool_pressure_status_payload(current if current else None),
        }

    def _publish_task_list_envelope(
        self,
        *,
        target_session_id: str,
        session_id: str,
        event_type: str,
        data: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        payload = build_envelope(
            channel='task',
            session_id=session_id,
            task_id=(str(task_id or '').strip() or None),
            seq=self.registry.next_task_list_seq(target_session_id),
            type=event_type,
            data=data,
        )
        self.registry.publish_task_list(target_session_id, payload)
        return payload

    def _callback_headers(self, *, token: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        normalized_token = str(token or '').strip()
        if normalized_token:
            headers['x-g3ku-internal-token'] = normalized_token
        return headers

    @staticmethod
    def _replace_internal_callback_path(url: str, *, target_path: str) -> str:
        text = str(url or '').strip()
        if not text:
            return ''
        parsed = urlparse(text)
        path = str(parsed.path or '').strip()
        if path.endswith(TASK_TERMINAL_CALLBACK_PATH):
            next_path = f'{path[: -len(TASK_TERMINAL_CALLBACK_PATH)]}{target_path}'
        elif not path or path == '/':
            next_path = target_path
        else:
            return ''
        return urlunparse(parsed._replace(path=next_path))

    def _internal_callback_targets(
        self,
        *,
        workspace: Path,
        callback_kind: str,
    ) -> list[tuple[str, str]]:
        normalized_kind = str(callback_kind or '').strip().lower()
        if normalized_kind == 'terminal':
            target_path = TASK_TERMINAL_CALLBACK_PATH
            primary_url = resolve_task_terminal_callback_url(workspace=workspace)
            primary_token = resolve_task_terminal_callback_token(workspace=workspace)
        elif normalized_kind == 'event':
            target_path = TASK_EVENT_CALLBACK_PATH
            primary_url = resolve_task_event_callback_url(workspace=workspace)
            primary_token = resolve_task_event_callback_token(workspace=workspace)
        elif normalized_kind == 'event_batch':
            target_path = TASK_EVENT_BATCH_CALLBACK_PATH
            primary_url = resolve_task_event_batch_callback_url(workspace=workspace)
            primary_token = resolve_task_event_callback_token(workspace=workspace)
        elif normalized_kind == 'stall':
            target_path = TASK_STALL_CALLBACK_PATH
            primary_url = resolve_task_stall_callback_url(workspace=workspace)
            primary_token = resolve_task_stall_callback_token(workspace=workspace)
        elif normalized_kind == 'distribution_error':
            target_path = TASK_DISTRIBUTION_ERROR_CALLBACK_PATH
            primary_url = resolve_task_distribution_error_callback_url(workspace=workspace)
            primary_token = resolve_task_distribution_error_callback_token(workspace=workspace)
        else:
            return []

        candidates: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _append(url: str, token: str) -> None:
            normalized_url = str(url or '').strip()
            normalized_token = str(token or '').strip()
            if not normalized_url:
                return
            item = (normalized_url, normalized_token)
            if item in seen:
                return
            seen.add(item)
            candidates.append(item)

        _append(primary_url, primary_token)

        file_config = load_task_terminal_callback_config(workspace=workspace)
        file_terminal_url = str(file_config.get('url') or '').strip()
        if target_path != TASK_TERMINAL_CALLBACK_PATH:
            file_url = self._replace_internal_callback_path(file_terminal_url, target_path=target_path)
        else:
            file_url = file_terminal_url
        _append(file_url, str(file_config.get('token') or '').strip())

        return candidates

    def _get_callback_client(self) -> httpx.AsyncClient:
        client = self._callback_client
        if client is None:
            client = httpx.AsyncClient(limits=self._callback_client_limits())
            self._callback_client = client
        return client

    @staticmethod
    def _callback_client_limits() -> httpx.Limits:
        return httpx.Limits(
            max_connections=_CALLBACK_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=_CALLBACK_POOL_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_CALLBACK_POOL_KEEPALIVE_EXPIRY_SECONDS,
        )

    def _loopback_web_port(self) -> int:
        """本进程内部回调要打到的那个 web 端口。"""
        for kind in ('event', 'terminal'):
            targets = self._internal_callback_targets(workspace=Path.cwd(), callback_kind=kind)
            for url, _token in targets:
                try:
                    port = urlparse(str(url or '')).port
                except ValueError:
                    continue
                if port:
                    return int(port)
        return 0

    def loopback_pressure_snapshot(self) -> dict[str, Any]:
        """回环连接读数：本进程挂在自家 web 端口上的 ESTABLISHED 条数。

        这是套接字耗尽（WinError 10055）唯一能提前看到的量——10055 本身要到
        0.25 次/分才出现一次，等它响已经太晚。超阈值时限流打一行存活任务名分组，
        用来点名是哪条车道在占连接。
        """
        port = self._loopback_web_port()
        established = 0
        if port:
            try:
                import psutil

                process = psutil.Process()
                getter = getattr(process, 'net_connections', None) or process.connections
                for conn in getter('tcp'):
                    remote = getattr(conn, 'raddr', None)
                    if remote is None:
                        continue
                    if int(getattr(remote, 'port', 0) or 0) == port and str(conn.status) == psutil.CONN_ESTABLISHED:
                        established += 1
            except Exception:
                established = 0
        now_mono = time.monotonic()
        if (
            established >= _LOOPBACK_CENSUS_WARN_CONNECTIONS
            and now_mono - self._loopback_census_logged_mono >= _LOOPBACK_CENSUS_INTERVAL_SECONDS
        ):
            self._loopback_census_logged_mono = now_mono
            try:
                prefixes: dict[str, int] = {}
                for task in asyncio.all_tasks():
                    name = str(task.get_name() or '')
                    key = name.split(':', 1)[0] or '(unnamed)'
                    prefixes[key] = prefixes.get(key, 0) + 1
                top = sorted(prefixes.items(), key=lambda item: -item[1])[:8]
                logger.warning(
                    'loopback connection census: established={} port={} live_tasks={}',
                    established,
                    port,
                    dict(top),
                )
            except Exception:
                pass
        stats = dict(self._task_event_stats)
        # 队列的字节水位（估）：光看条数看不出一批 MB 级摘要和一批几 KB 事实的区别，
        # 而夜里打满 4096 那次就是靠条数闸门才没提前露出来。
        estimated_bytes, _counts = self._task_event_queue_pressure(self._task_event_pending)
        stats['task_event_queue_bytes_estimated'] = float(estimated_bytes)
        stats.update({
            'loopback_established_count': float(established),
            'loopback_web_port': float(port),
        })
        stats.update(self._callback_pool_snapshot())
        return stats

    def _callback_pool_snapshot(self) -> dict[str, float]:
        """共享回调客户端的连接池读数——只有这两个数能分"被借走没还"和"空闲没修剪"。

        实盘现场：worker→web 的 ESTABLISHED 单调爬到 32/67/101，而 census 的挂起点榜显示
        回调车道上 0 个任务在等 ⇒ socket 不在服务任何请求。httpcore 的池要么留着空闲连接
        等下次借出时修剪（那时 `is_available()` 为真），要么被一个再也没回来的请求占着
        （`is_available()` 恒假）。前者该设 `Limits`，后者必须修取消路径，
        把 `max_connections` 调小只会把泄漏换成静默丢事件。
        """
        snapshot = {
            'loopback_pool_connections': -1.0,
            'loopback_pool_leased': -1.0,
        }
        transport = getattr(self._callback_client, '_transport', None)
        pool = getattr(transport, '_pool', None)
        connections = getattr(pool, 'connections', None)
        if connections is None:
            return snapshot
        leased = 0
        for connection in connections:
            try:
                if not bool(connection.is_available()):
                    leased += 1
            except Exception:
                continue
        snapshot['loopback_pool_connections'] = float(len(connections))
        snapshot['loopback_pool_leased'] = float(leased)
        return snapshot

    async def _post_internal_callback(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        allowed, reason = is_allowed_callback_url(url)
        if not allowed:
            logger.error(
                'internal callback URL not allowed, skip delivery: url_host={} reason={}',
                _callback_host_for_log(url),
                reason,
            )
            raise CallbackUrlNotAllowedError(f'internal callback URL not allowed: {reason}')
        client = self._get_callback_client()
        # 四档一起用调用方给的值，不给 connect 单独放宽：`db724884` 试过把 connect 抬到
        # 5 s，判据当场被自己的计数否掉——上线后 ConnectTimeout 仍 35 次/7 分钟（≈5 次/分，
        # 与抬档前同一量级），因为这条超时是**被卡住的同一个循环**判的：循环停 6 s 时
        # 5 s 的预算也只是换个更晚的时刻输。放宽只让排空口多等 3.5 s。
        try:
            return await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=float(timeout or _WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_callback_delivery_error(type(exc).__name__)
            raise

    def _note_callback_delivery_error(self, error_type: str) -> None:
        """回调投递异常计数 + 限流一行，用来把这条车道的失败从 debug 里捞出来。

        实盘回环 socket 单调爬（20:18 起 3 条/分）而池里只认 5 条，说明有连接被池
        丢掉却没关；这类形状一定先在某个投递异常上露头，计数是唯一能证明它的方式。
        """
        counts = self._callback_delivery_errors
        counts[error_type] = counts.get(error_type, 0) + 1
        self._task_event_stats['callback_delivery_error_count'] = float(sum(counts.values()))
        now_mono = time.monotonic()
        if now_mono - self._callback_error_logged_mono < _CALLBACK_ERROR_WARN_INTERVAL_SECONDS:
            return
        self._callback_error_logged_mono = now_mono
        logger.warning(
            'internal callback delivery errors: total={} by_type={} pool={}',
            int(sum(counts.values())),
            dict(sorted(counts.items(), key=lambda item: -item[1])[:6]),
            self._callback_pool_snapshot(),
        )

    def _schedule_task_event_callback(self, payload: dict[str, Any] | None) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(payload)
        if not normalized:
            return
        if str(normalized.get('event_type') or '').strip() == 'task.summary.patch':
            self._enqueue_task_summary_callback(normalized)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._enqueue_task_event_callback(normalized, loop)

    def _enqueue_task_event_callback(self, payload: dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
        pending = self._task_event_pending
        event_type = str(payload.get('event_type') or '').strip()
        if event_type in _TASK_EVENT_COALESCED_EVENT_TYPES:
            slot = self._find_task_event_coalesce_slot(pending, payload)
            if slot is not None:
                # 一份 live.patch 载荷就是该任务的全量运行时摘要（实盘单任务 4.3 MB）。
                # 排空口每 0.25 s 只能投一条（合批按 64 KB 封顶，MB 级载荷永远凑不进第二条），
                # 生产端比它快时旧份只是占着队列的过期状态：就地换成最新一份，位置不变。
                pending[slot] = payload
                self._task_event_stats['task_event_coalesced_count'] += 1.0
                self._ensure_task_event_flush_task(loop)
                return
        pressure_due = (
            len(pending) >= _TASK_EVENT_QUEUE_PRESSURE_ITEMS
            or bool(self._task_event_heavy_types)
        )
        estimated_bytes, counts = self._task_event_queue_pressure(pending) if pressure_due else (0, {})
        if len(pending) >= _TASK_EVENT_QUEUE_MAX_ITEMS or estimated_bytes > _TASK_EVENT_QUEUE_MAX_BYTES:
            # 优先扔**最旧的可合类**（`task.live.patch` 按定义就是"该任务当前全量状态"，
            # 旧一份永远会被新一份覆盖，留着只是占字节的过期视图）；没有可扔的才回到
            # 原规则。原规则只按条数封顶，夜里打出过 18715 条无名丢弃。
            victim = next(
                (index for index, item in enumerate(pending)
                 if str(item.get('event_type') or '').strip() in _TASK_EVENT_COALESCED_EVENT_TYPES),
                None,
            )
            if victim is not None:
                evicted = pending.pop(victim)
                self._note_task_event_drop(
                    pending, counts,
                    event_type=event_type,
                    evicted_type=str(evicted.get('event_type') or '').strip(),
                    estimated_bytes=estimated_bytes,
                )
            elif event_type != 'task.terminal':
                # 满了丢最新一条非终态事件（维持原语义），但要能点名丢的是哪一类。
                self._note_task_event_drop(
                    pending, counts, event_type=event_type, evicted_type='', estimated_bytes=estimated_bytes,
                )
                return
            else:
                # 终态事件必须挤出一个位置，丢了任务就永不收口。
                dropped = next(
                    (index for index, item in enumerate(pending)
                     if str(item.get('event_type') or '').strip() != 'task.terminal'),
                    None,
                )
                if dropped is not None:
                    evicted = pending.pop(dropped)
                    self._note_task_event_drop(
                        pending, counts,
                        event_type=event_type,
                        evicted_type=str(evicted.get('event_type') or '').strip(),
                        estimated_bytes=estimated_bytes,
                    )
        pending.append(payload)
        self._task_event_stats['task_event_queued_count'] = float(len(pending))
        self._ensure_task_event_flush_task(loop)

    def _task_event_queue_pressure(self, pending: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
        """(估算字节数, 按类型的队列组成)。只在危险区调用，一次 O(n) 扫描。

        字节数是**估**的：每类的平均载荷大小由排空口本来就算过的 `encoded_size` 学出来
        （EWMA），没见过的类按 `_TASK_EVENT_UNKNOWN_ITEM_BYTES` 计。入队路径上绝不再
        `json.dumps` 一遍——那正是这一串性能工作砍掉的开销。
        """
        counts: dict[str, int] = {}
        total = 0
        for item in pending:
            key = str(item.get('event_type') or '').strip() or '(unnamed)'
            counts[key] = counts.get(key, 0) + 1
            total += self._task_event_avg_item_bytes.get(key, _TASK_EVENT_UNKNOWN_ITEM_BYTES)
        return total, counts

    def _note_task_event_drop(
        self,
        pending: list[dict[str, Any]],
        counts: dict[str, int],
        *,
        event_type: str,
        evicted_type: str,
        estimated_bytes: int,
    ) -> None:
        key = (evicted_type or event_type or '(unnamed)').strip() or '(unnamed)'
        self._task_event_dropped_by_type[key] = self._task_event_dropped_by_type.get(key, 0) + 1
        stats = self._task_event_stats
        stats['task_event_dropped_count'] += 1.0
        if key in _TASK_EVENT_COALESCED_EVENT_TYPES:
            stats['task_event_dropped_coalesced_count'] += 1.0
        else:
            stats['task_event_dropped_other_count'] += 1.0
        now_mono = time.monotonic()
        if now_mono - self._task_event_drop_logged_mono < _TASK_EVENT_DROP_WARN_INTERVAL_SECONDS:
            return
        self._task_event_drop_logged_mono = now_mono
        logger.warning(
            'task event queue over budget: depth={} estimated_bytes={} queued_by_type={} dropped_by_type={}',
            len(pending),
            estimated_bytes,
            dict(sorted(counts.items(), key=lambda item: -item[1])[:6]),
            dict(sorted(self._task_event_dropped_by_type.items(), key=lambda item: -item[1])[:6]),
        )

    def _note_task_event_delivery_drop(self, payload: dict[str, Any]) -> None:
        """所有回调目标都没送到 = 这一帧真丢了，所以必须可数、能点名是哪一类。

        原来这里只有 `logger.debug('task event callback delivery skipped')`：判"丢了几帧"只能
        从 `callback_delivery_error_count` 反推，而那个数的是投递异常次数——一次批量失败回落
        逐条、逐条再失败，同一帧在里面算两笔。带上池读数是故意的：实盘 ConnectTimeout 全部与
        事件循环停顿同拍（09:47:28 lag=4113ms 时池里 1 条且 leased=1），一眼分得开"循环卡了"
        和"池满了"。
        """
        key = str(payload.get('event_type') or '').strip() or '(unnamed)'
        dropped = self._task_event_delivery_dropped_by_type
        dropped[key] = int(dropped.get(key) or 0) + 1
        self._task_event_stats['task_event_delivery_dropped_count'] += 1.0
        now_mono = time.monotonic()
        if now_mono - self._task_event_delivery_logged_mono < _CALLBACK_ERROR_WARN_INTERVAL_SECONDS:
            return
        self._task_event_delivery_logged_mono = now_mono
        logger.warning(
            'task event callback delivery dropped (rate-limited): latest={} by_type={} pool={}',
            key,
            dict(sorted(dropped.items(), key=lambda item: -item[1])[:6]),
            self._callback_pool_snapshot(),
        )

    @staticmethod
    def _find_task_event_coalesce_slot(
        pending: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> int | None:
        """从队尾往前找同任务同类型的旧载荷槽位；撞到别的同任务事件就放弃（不许跨过它）。"""
        task_id = str(payload.get('task_id') or '').strip()
        event_type = str(payload.get('event_type') or '').strip()
        if not task_id or not event_type:
            return None
        for index in range(len(pending) - 1, -1, -1):
            item = pending[index]
            if str(item.get('task_id') or '').strip() != task_id:
                continue
            if str(item.get('event_type') or '').strip() == event_type:
                return index
            return None
        return None

    def _ensure_task_event_flush_task(self, loop: asyncio.AbstractEventLoop) -> None:
        current = self._task_event_flush_task
        if current is not None and not current.done():
            return
        self._task_event_flush_task = loop.create_task(
            self._deliver_task_event_batches(),
            name=f'main-runtime-task-event-batch:{self.worker_id or "worker"}',
        )

    async def _deliver_task_event_batches(self) -> None:
        while self._task_event_pending:
            await asyncio.sleep(_TASK_EVENT_BATCH_WINDOW_SECONDS)
            batch = self._drain_task_event_batch()
            if not batch:
                continue
            callback_targets = self._internal_callback_targets(
                workspace=Path.cwd(), callback_kind='event_batch'
            )
            delivered = False
            if callback_targets:
                headers = self._callback_headers(token=str(callback_targets[0][1] or ''))
                try:
                    response = await self._post_internal_callback(
                        callback_targets[0][0],
                        payload={'items': batch},
                        headers=headers,
                        timeout=_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS,
                    )
                    delivered = 200 <= int(response.status_code or 0) < 300
                except asyncio.CancelledError:
                    raise
                except Exception:
                    delivered = False
                if delivered:
                    self._task_event_stats['task_event_batch_request_count'] += 1.0
                    self._task_event_stats['task_event_batch_item_count'] += float(len(batch))
            if not delivered:
                # 批量口不可用（旧版 web 只认 summary.patch）时逐条回落，语义与改动前一致。
                self._task_event_stats['task_event_single_fallback_count'] += float(len(batch))
                for item in batch:
                    await self._deliver_task_event_callback(item)
            self._task_event_stats['task_event_queued_count'] = float(len(self._task_event_pending))

    def _drain_task_event_batch(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        bytes_total = 0
        while self._task_event_pending and len(batch) < _TASK_EVENT_BATCH_MAX_ITEMS:
            payload = self._task_event_pending[0]
            encoded_size = len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8'))
            if batch and bytes_total + encoded_size > _TASK_EVENT_BATCH_MAX_BYTES:
                break
            self._task_event_pending.pop(0)
            batch.append(payload)
            bytes_total += encoded_size
            # 顺手学会"这一类平均多大"：入队侧的字节水位要靠它估，而这里是本来就算好的。
            type_key = str(payload.get('event_type') or '').strip() or '(unnamed)'
            previous = self._task_event_avg_item_bytes.get(type_key)
            self._task_event_avg_item_bytes[type_key] = (
                int((previous * 3 + encoded_size) / 4) if previous else encoded_size
            )
            if self._task_event_avg_item_bytes[type_key] >= _TASK_EVENT_HEAVY_ITEM_BYTES:
                self._task_event_heavy_types.add(type_key)
        return batch

    async def _deliver_task_event_callback(self, payload: dict[str, Any]) -> None:
        callback_targets = self._internal_callback_targets(workspace=Path.cwd(), callback_kind='event')
        if not callback_targets:
            return
        for index, (callback_url, callback_token) in enumerate(callback_targets):
            headers = self._callback_headers(token=callback_token)
            try:
                response = await self._post_internal_callback(
                    callback_url,
                    payload=payload,
                    headers=headers,
                    timeout=1.5,
                )
                if 200 <= int(response.status_code or 0) < 300:
                    return
                if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                if index + 1 < len(callback_targets):
                    continue
                continue
            break
        self._note_task_event_delivery_drop(payload)

    def publish_worker_status_event(
        self,
        *,
        item: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        bridge: bool = False,
    ) -> dict[str, object]:
        data = dict(payload or self.worker_status_payload(item=item))
        channels = self.registry.task_list_channels() or ['all']
        for channel in channels:
            self._publish_task_list_envelope(
                target_session_id=channel,
                session_id=channel,
                event_type='task.worker.status',
                data=data,
            )
        if self.execution_mode == 'worker' and not bridge:
            self._enqueue_task_worker_status_callback(data)
        return data

    def _publish_task_list_patch_event(
        self,
        *,
        session_id: str,
        task_payload: dict[str, Any],
        bridge: bool = False,
    ) -> None:
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        normalized_task_payload = dict(task_payload or {})
        normalized_task_id = self.normalize_task_id(
            str(
                normalized_task_payload.get('task_id')
                or normalized_task_payload.get('taskId')
                or ''
            ).strip()
        )
        normalized_task_payload['task_id'] = normalized_task_id
        data = {'task': normalized_task_payload}
        for target_session_id in {normalized_session_id, 'all'}:
            self._publish_task_list_envelope(
                target_session_id=target_session_id,
                session_id=normalized_session_id,
                task_id=normalized_task_id,
                event_type='task.summary.patch',
                data=data,
            )
        if self.execution_mode == 'worker' and not bridge:
            self._schedule_task_event_callback(
                {
                    'event_type': 'task.summary.patch',
                    'session_id': normalized_session_id,
                    'task_id': normalized_task_id,
                    'data': data,
                }
            )

    def _publish_task_deleted_event(self, *, session_id: str, task_id: str) -> None:
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        normalized_task_id = self.normalize_task_id(task_id)
        data = {'task_id': normalized_task_id}
        for target_session_id in {normalized_session_id, 'all'}:
            self._publish_task_list_envelope(
                target_session_id=target_session_id,
                session_id=normalized_session_id,
                task_id=normalized_task_id,
                event_type='task.deleted',
                data=data,
            )
        payload = build_envelope(
            channel='task',
            session_id=normalized_session_id,
            task_id=normalized_task_id,
            seq=self.registry.next_global_task_seq(normalized_task_id),
            type='task.deleted',
            data=data,
        )
        self.registry.publish_global_task(normalized_task_id, payload)

    def _publish_task_artifact_applied_event(self, *, task: TaskRecord, artifact_id: str, path: str) -> None:
        payload = build_envelope(
            channel='task',
            session_id=task.session_id,
            task_id=task.task_id,
            seq=self.registry.next_global_task_seq(task.task_id),
            type='task.artifact.applied',
            data={'artifact_id': artifact_id, 'path': path, 'applied': True, 'task_id': task.task_id},
        )
        self.registry.publish_global_task(task.task_id, payload)

    def _publish_live_snapshot(self, task: TaskRecord, payload: dict[str, Any], publish_summary: bool) -> None:
        started_at = now_iso()
        started_mono = time.perf_counter()
        event_type = str(payload.get('event_type') or '').strip()
        data = dict(payload.get('data') or {})
        if event_type == 'task.summary.patch':
            if self.execution_mode == 'worker':
                self._enqueue_task_summary_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    },
                    immediate=bool(payload.get('dispatch_immediate')),
                )
                self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
                return
            self._publish_task_list_envelope(
                target_session_id=task.session_id,
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            self._publish_task_list_envelope(
                target_session_id='all',
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            detail_payload = build_envelope(
                channel='task',
                session_id=task.session_id,
                task_id=task.task_id,
                seq=self.registry.next_global_task_seq(task.task_id),
                type=event_type,
                data=data,
            )
            self.registry.publish_global_task(task.task_id, detail_payload)
            self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
            return
        if event_type == 'task.token.patch':
            if self.execution_mode == 'worker':
                self._schedule_task_event_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    }
                )
                self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
                return
            self._publish_task_list_envelope(
                target_session_id=task.session_id,
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            self._publish_task_list_envelope(
                target_session_id='all',
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
            return
        if event_type in {'task.node.patch', 'task.live.patch', 'task.model.call', 'task.terminal'}:
            detail_payload = build_envelope(
                channel='task',
                session_id=task.session_id,
                task_id=task.task_id,
                seq=self.registry.next_global_task_seq(task.task_id),
                type=event_type,
                data=data,
            )
            self.registry.publish_global_task(task.task_id, detail_payload)
            if self.execution_mode == 'worker':
                self._schedule_task_event_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    }
                )
            self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
            return
        self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
        return

    def forward_live_task_event(self, payload: dict[str, Any] | None) -> bool:
        normalized = normalize_task_event_payload(payload)
        if not normalized:
            return False
        event_type = str(normalized.get('event_type') or '').strip()
        session_id = str(normalized.get('session_id') or 'web:shared').strip() or 'web:shared'
        task_id = self.normalize_task_id(str(normalized.get('task_id') or '').strip()) if normalized.get('task_id') else ''
        data = dict(normalized.get('data') or {})
        if event_type in {'task.node.patch', 'task.live.patch', 'task.model.call', 'task.terminal'} and task_id:
            payload = build_envelope(
                channel='task',
                session_id=session_id,
                task_id=task_id,
                seq=self.registry.next_global_task_seq(task_id),
                type=event_type,
                data=data,
            )
            self.registry.publish_global_task(task_id, payload)
            return True
        if event_type == 'task.token.patch' and task_id:
            for target_session_id in {session_id, 'all'}:
                self._publish_task_list_envelope(
                    target_session_id=target_session_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type='task.token.patch',
                    data=data,
                )
            return True
        if event_type == 'task.summary.patch':
            normalized_task_payload = dict(data.get('task') or {})
            normalized_task_id = self.normalize_task_id(str(normalized_task_payload.get('task_id') or task_id or '').strip())
            normalized_task_payload['task_id'] = normalized_task_id
            for target_session_id in {session_id, 'all'}:
                self._publish_task_list_envelope(
                    target_session_id=target_session_id,
                    session_id=session_id,
                    task_id=normalized_task_id,
                    event_type='task.summary.patch',
                    data={'task': normalized_task_payload},
                )
            payload = build_envelope(
                channel='task',
                session_id=session_id,
                task_id=normalized_task_id,
                seq=self.registry.next_global_task_seq(normalized_task_id),
                type='task.summary.patch',
                data={'task': normalized_task_payload},
            )
            self.registry.publish_global_task(normalized_task_id, payload)
            return True
        if event_type == 'task.worker.status':
            self.publish_worker_status_event(
                item=dict(data.get('worker') or {}) if isinstance(data.get('worker'), dict) else None,
                bridge=True,
            )
            return True
        return False

    def _assert_worker_available(self) -> None:
        if self.execution_mode != 'web':
            return
        state = self.worker_state()
        if state == _WORKER_STATE_ONLINE:
            return
        if state == _WORKER_STATE_STARTING:
            raise ValueError('task_worker_starting')
        if state == _WORKER_STATE_STALE:
            raise ValueError('task_worker_stale')
        raise ValueError('task_worker_offline')

    @staticmethod
    def _normalize_session_key(session_id: str | None) -> str:
        return str(session_id or 'web:shared').strip() or 'web:shared'

    def _task_origin_session_id(self, task: TaskRecord | None) -> str:
        if task is None:
            return 'web:shared'
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        origin_session_id = str(metadata.get('origin_session_id') or '').strip()
        if origin_session_id:
            return origin_session_id
        return self._normalize_session_key(getattr(task, 'session_id', 'web:shared'))

    def list_tasks_for_session(self, session_id: str) -> list[TaskRecord]:
        key = self._normalize_session_key(session_id)
        return [task for task in self.store.list_tasks() if self._task_origin_session_id(task) == key]

    def list_unfinished_tasks_for_session(self, session_id: str) -> list[TaskRecord]:
        return [
            task
            for task in self.list_tasks_for_session(session_id)
            if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress'
        ]

    def list_finished_tasks_for_session_since(
        self, session_id: str, since_iso: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        reader = getattr(self.store, 'list_finished_tasks_for_session_since', None)
        if not callable(reader):
            return []
        return list(reader(str(session_id or '').strip(), str(since_iso or '').strip(), limit=limit) or [])

    def list_task_node_activity(self, task_id: str, *, limit: int = 6) -> list[dict[str, Any]]:
        reader = getattr(self.store, 'list_task_node_activity', None)
        if not callable(reader):
            return []
        return list(reader(str(task_id or '').strip(), limit=limit) or [])

    def count_task_nodes(self, task_id: str) -> int:
        counter = getattr(self.store, 'count_task_nodes', None)
        if not callable(counter):
            return 0
        return int(counter(str(task_id or '').strip()) or 0)

    def _refresh_task_store_read_snapshot(self) -> None:
        refresher = getattr(self.store, 'refresh_read_snapshot', None)
        if not callable(refresher):
            return
        try:
            refresher()
        except Exception:
            logger.debug('task store read snapshot refresh skipped')

    @staticmethod
    def _normalize_async_task_target_text(value: Any) -> str:
        text = str(value or '').strip().casefold()
        if not text:
            return ''
        text = re.sub(r'\s+', ' ', text)
        return (
            text.replace('，', ',')
            .replace('。', '.')
            .replace('：', ':')
            .replace('（', '(')
            .replace('）', ')')
        )

    def _async_task_keyword_fingerprint(self, value: Any) -> tuple[str, ...]:
        normalized = self._normalize_async_task_target_text(value)
        if not normalized:
            return ()
        tokens = re.findall(r"[a-z0-9_:/.-]+|[\u4e00-\u9fff]+", normalized)
        unique_tokens: list[str] = []
        for raw_token in tokens:
            token = str(raw_token or '').strip()
            if len(token) <= 1 or token in unique_tokens:
                continue
            unique_tokens.append(token)
        return tuple(unique_tokens)

    def _async_task_precheck_pool(self, session_id: str) -> list[dict[str, Any]]:
        """重复预检候选池：只收集正在运行的任务。

        已暂停（is_paused）或已进入终态（success/failed）的任务不参与重复匹配——
        创建者只需暂停旧任务或等待其失败，即可重新创建同需求的任务。
        """
        self._refresh_task_store_read_snapshot()
        pool: list[dict[str, Any]] = []
        for task in self.list_unfinished_tasks_for_session(session_id):
            if bool(getattr(task, 'is_paused', False)):
                continue
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            task_text = str(getattr(task, 'user_request', '') or '').strip()
            core_requirement = str(metadata.get('core_requirement') or '').strip()
            target_text = core_requirement or task_text
            pool.append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'task_text': task_text,
                    'core_requirement': core_requirement,
                    'execution_policy': dict(metadata.get('execution_policy') or {}),
                    'status': str(getattr(task, 'status', '') or '').strip(),
                    'is_paused': bool(getattr(task, 'is_paused', False)),
                    'target_text': self._normalize_async_task_target_text(target_text),
                    'keyword_fingerprint': self._async_task_keyword_fingerprint(target_text),
                }
            )
        return pool

    def _rule_precheck_async_task_creation(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any]:
        _ = execution_policy, requires_final_acceptance, final_acceptance_prompt
        candidate_target = self._normalize_async_task_target_text(core_requirement or task_text)
        candidate_keywords = self._async_task_keyword_fingerprint(core_requirement or task_text)
        matched_task_ids: list[str] = []
        match_kinds: list[str] = []
        for item in self._async_task_precheck_pool(session_id):
            task_id = str(item.get('task_id') or '').strip()
            if not task_id or task_id in matched_task_ids:
                continue
            if candidate_target and candidate_target == str(item.get('target_text') or '').strip():
                matched_task_ids.append(task_id)
                if 'core_requirement exact match' not in match_kinds:
                    match_kinds.append('core_requirement exact match')
                continue
            if candidate_keywords and candidate_keywords == tuple(item.get('keyword_fingerprint') or ()):
                matched_task_ids.append(task_id)
                if 'keyword fingerprint exact match' not in match_kinds:
                    match_kinds.append('keyword fingerprint exact match')
        if matched_task_ids:
            return {
                'decision': 'reject_duplicate',
                'matched_task_id': matched_task_ids[0],
                'matched_task_ids': matched_task_ids,
                'reason': '; '.join(match_kinds),
                'decision_source': 'rule',
            }
        return {
            'decision': 'approve_new',
            'matched_task_id': '',
            'matched_task_ids': [],
            'reason': 'rule precheck found no exact duplicate',
            'decision_source': 'rule',
        }

    def _running_async_task_review_payload(self, session_id: str) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for item in self._async_task_precheck_pool(session_id):
            payload.append(
                {
                    'task_id': str(item.get('task_id') or '').strip(),
                    'task_text': str(item.get('task_text') or '').strip(),
                    'core_requirement': str(item.get('core_requirement') or '').strip(),
                    'execution_policy': dict(item.get('execution_policy') or {}),
                    'status': str(item.get('status') or '').strip(),
                }
            )
        return payload

    @staticmethod
    def _parse_async_task_duplicate_precheck_response(response: Any) -> dict[str, Any] | None:
        tool_calls = list(getattr(response, 'tool_calls', []) or [])
        for call in tool_calls:
            if isinstance(call, dict):
                name = str(call.get('name') or '').strip()
                arguments: Any = call.get('arguments')
            else:
                name = str(getattr(call, 'name', '') or '').strip()
                arguments = getattr(call, 'arguments', None)
            if name != 'review_async_task_duplicate_precheck':
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    return None
            if not isinstance(arguments, dict):
                return None
            decision = str(arguments.get('decision') or '').strip()
            if decision not in {'approve_new', 'reject_duplicate', 'reject_use_append_notice'}:
                return None
            matched_task_id = str(arguments.get('matched_task_id') or '').strip()
            if decision != 'approve_new' and not matched_task_id.startswith('task:'):
                return None
            return {
                'decision': decision,
                'matched_task_id': matched_task_id,
                'reason': str(arguments.get('reason') or '').strip(),
                'decision_source': 'llm',
            }
        return None

    async def _execute_async_task_duplicate_precheck_review(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any] | None:
        model_refs = list(self.node_runner._acceptance_model_refs or self.node_runner._execution_model_refs)
        if not model_refs:
            return None
        backend = self._chat_backend
        if backend is None or not callable(getattr(backend, 'chat', None)):
            return None
        response = await backend.chat(
            messages=[
                {'role': 'system', 'content': load_prompt('async_task_duplicate_precheck.md').strip()},
                {
                    'role': 'user',
                    'content': json.dumps(
                        {
                            'candidate_task': {
                                'task_text': str(task_text or '').strip(),
                                'core_requirement': str(core_requirement or '').strip(),
                                'execution_policy': dict(execution_policy or {}),
                                'requires_final_acceptance': bool(requires_final_acceptance),
                                'final_acceptance_prompt': str(final_acceptance_prompt or '').strip(),
                            },
                            'running_session_tasks': self._running_async_task_review_payload(session_id),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],
            tools=[
                {
                    'type': 'function',
                    'function': {
                        'name': 'review_async_task_duplicate_precheck',
                        'description': 'Decide whether a new async task should be created or blocked.',
                        'parameters': {
                            'type': 'object',
                            'properties': {
                                'decision': {
                                    'type': 'string',
                                    'enum': ['approve_new', 'reject_duplicate', 'reject_use_append_notice'],
                                },
                                'matched_task_id': {'type': 'string'},
                                'reason': {'type': 'string'},
                            },
                            'required': ['decision', 'reason'],
                            'additionalProperties': False,
                        },
                    },
                }
            ],
            model_refs=model_refs,
        )
        return self._parse_async_task_duplicate_precheck_response(response)

    async def precheck_async_task_creation(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_key(session_id)
        rule_decision = self._rule_precheck_async_task_creation(
            session_id=normalized_session_id,
            task_text=task_text,
            core_requirement=core_requirement,
            execution_policy=dict(execution_policy or {}),
            requires_final_acceptance=bool(requires_final_acceptance),
            final_acceptance_prompt=str(final_acceptance_prompt or '').strip(),
        )
        if str(rule_decision.get('decision') or '').strip() != 'approve_new':
            return rule_decision
        if not self._async_task_precheck_pool(normalized_session_id):
            return rule_decision
        if not self._duplicate_precheck_llm_review_enabled:
            return {
                'decision': 'approve_new',
                'matched_task_id': '',
                'matched_task_ids': [],
                'reason': 'llm review disabled; rule precheck only',
                'decision_source': 'rule',
            }
        try:
            llm_decision = await self._execute_async_task_duplicate_precheck_review(
                session_id=normalized_session_id,
                task_text=task_text,
                core_requirement=core_requirement,
                execution_policy=dict(execution_policy or {}),
                requires_final_acceptance=bool(requires_final_acceptance),
                final_acceptance_prompt=str(final_acceptance_prompt or '').strip(),
            )
        except Exception:
            llm_decision = None
        if llm_decision is None:
            return {
                'decision': 'approve_new',
                'matched_task_id': '',
                'reason': 'llm review unavailable; allow new task',
                'decision_source': 'fallback',
            }
        return llm_decision

    def revalidate_async_task_creation_before_create(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_key(session_id)
        self._refresh_task_store_read_snapshot()
        return self._rule_precheck_async_task_creation(
            session_id=normalized_session_id,
            task_text=task_text,
            core_requirement=core_requirement,
            execution_policy=dict(execution_policy or {}),
            requires_final_acceptance=bool(requires_final_acceptance),
            final_acceptance_prompt=str(final_acceptance_prompt or '').strip(),
        )

    # ------------------------------------------------------------------
    # 节点信箱直投 + R2 唤醒（不走 distribution epoch）
    # ------------------------------------------------------------------

    def _frame_phase(self, task_id: str, node_id: str) -> str:
        try:
            frame = self.store.get_task_runtime_frame(task_id, node_id)
        except Exception:
            return ''
        return str(getattr(frame, 'phase', '') or '') if frame is not None else ''

    def node_park_snapshot(self, task_id: str, node_id: str) -> tuple[Any, str]:
        """`(node, park_state)` for one node; park_state 与唤醒判定同源。"""
        node = self.store.get_node(node_id)
        if node is None:
            return None, ''
        state = node_park_state(
            status=str(getattr(node, 'status', '') or ''),
            is_paused=bool(getattr(node, 'is_paused', False) or getattr(node, 'pause_requested', False)),
            phase=self._frame_phase(task_id, node_id),
        )
        return node, state

    def node_wake_deps(self) -> WakeDeps:
        async def _resume(task_id: str, node_id: str) -> str:
            return await self.task_actor_service.resume_node_entry(task_id, node_id)

        async def _enqueue(task_id: str) -> None:
            await self.global_scheduler.enqueue_task(task_id)

        async def _clear_pause(task_id: str, node_id: str) -> None:
            # 清标志不等于恢复：它只是让执行器能起跑，真正复活由 resume 负责。
            self.log_service.set_node_pause_state(
                task_id,
                node_id,
                pause_requested=False,
                is_paused=False,
                pause_reason='',
                remark='',
            )

        return WakeDeps(
            get_node=self.store.get_node,
            get_phase=self._frame_phase,
            get_task=self.get_task,
            dispatcher_for=lambda task_id: self.task_actor_service._dispatchers.get(task_id),
            resume_node_entry=_resume,
            enqueue_task=_enqueue,
            clear_pause=_clear_pause,
        )

    async def wake_node_on_message(self, *, task_id: str, node_id: str) -> str:
        blocker = getattr(self.task_actor_service, 'web_role_blocked', None)
        if self.execution_mode == 'web' and callable(blocker) and blocker():
            # web 角色在 worker 在线时没有任务执行体：本地 enqueue 会拉起第二个
            # task actor，与 worker 并发驱动同一棵树。唤醒命令交给 worker 执行。
            task = self.get_task(task_id)
            command_id = self._enqueue_task_command(
                command_type='resume_node',
                task_id=task_id,
                session_id=self._task_origin_session_id(task),
                payload={'node_ids': [node_id]},
            )
            logger.info(
                'node wake delegated to worker: task={} node={} command={}',
                task_id,
                node_id,
                command_id,
            )
            return 'delegated_worker'
        outcome = await wake_parked_node(task_id=task_id, node_id=node_id, deps=self.node_wake_deps())
        try:
            logger.info('node wake: task={} node={} outcome={}', task_id, node_id, outcome)
        except Exception:
            pass
        return outcome

    def _delivery_wakes_target(
        self,
        *,
        task_id: str,
        node_id: str,
        node: Any,
        priority: str,
        park_state: str = '',
        sender_origin: str = '',
    ) -> bool:
        """停靠策略：任何可注入未读都会唤醒，只有一档例外。

        例外由 `main/runtime/node_mailbox.delivery_defers_wake` 判：人发给暂停
        节点的消息只落箱，唤醒会把操作员按下的暂停位顺手清掉。

        `priority` 保留在签名里是因为调用方与测试仍按投递判定传值；批次等待是
        落箱时的 held 状态，不在这里判。
        """
        _ = task_id, node_id, node, priority
        if delivery_defers_wake(target_park_state=park_state, sender_origin=sender_origin):
            return False
        return True

    @staticmethod
    def _delivery_to_waiting_acceptance_allowed(*, origin: str, relation: str) -> bool:
        """用户/前门/父级节点给正在等验收的执行节点发消息时允许落箱。

        验收本身仍在进行，所以这条消息不唤醒目标；它只是排队，等驳回把执行节点
        带回执行态后才被消费。peer/child 的消息仍是执行期上下文，维持拒收。
        """
        normalized_origin = str(origin or '').strip().lower()
        if normalized_origin in {ORIGIN_USER, ORIGIN_FRONTDOOR}:
            return True
        return (
            normalized_origin == ORIGIN_NODE
            and str(relation or '').strip().lower() == RELATION_PARENT
        )

    async def deliver_node_message(
        self,
        *,
        task_id: str,
        target_node_id: str,
        message: str,
        origin: str = 'node',
        relation: str = '',
        system_kind: str = '',
        source_node_id: str = '',
        verdict: str = '',
        ref: str = '',
        submission_id: str = '',
        epoch_id: str = '',
        wake: bool = True,
        held: bool = False,
        batch_id: str = '',
    ) -> dict[str, Any]:
        """一条定向投递：判定矩阵 → 机械推导 → 指纹去重 → 落箱 → 唤醒。

        `origin`/`relation`/`system_kind` 决定 kind；`system_kind` 只有运行时能传。

        `wake=False` 用于「必须留痕、但不得叫醒收件人」的系统消息：pass 裁决的
        收件人即将终局，reject_no_progress 裁决的收件人刚被判暂停（唤醒会立刻清掉
        暂停位）。消息照常落箱并带 kind/priority，只是不触发 R2 唤醒。
        同一档后果还有一条不走这个参数：人发给暂停节点的消息由
        `_delivery_wakes_target` 内部按 `delivery_defers_wake` 免唤醒，落箱待操作员恢复。
        `held=True` 用于 wait_all_children 批次：行照常落箱，但在批次释放前既不
        计入待注入也不唤醒。
        """
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(str(target_node_id or '').strip())
        node, park_state = self.node_park_snapshot(normalized_task_id, normalized_node_id)
        if node is None:
            return {'target': normalized_node_id, 'status': 'rejected', 'reason': 'target_missing'}

        target_kind = str(getattr(node, 'node_kind', '') or '').strip().lower()
        handshake = normalize_acceptance_handshake(
            ((node.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) if isinstance(node.metadata, dict) else {})
        )
        target_under_acceptance = (
            target_kind == 'execution'
            and str(handshake.get('state') or '').strip() == ACCEPTANCE_STATE_WAITING_ACCEPTANCE
        )
        under_acceptance_notice = ''
        if target_under_acceptance and not str(system_kind or '').strip():
            if not self._delivery_to_waiting_acceptance_allowed(origin=origin, relation=relation):
                return {
                    'target': normalized_node_id,
                    'status': 'rejected',
                    'reason': 'target_under_acceptance',
                    'target_state': 'waiting_acceptance',
                }
            under_acceptance_notice = _WAITING_ACCEPTANCE_DELIVERY_NOTICE
        under_acceptance_fields = (
            {'target_state': 'waiting_acceptance', 'notice': under_acceptance_notice}
            if under_acceptance_notice
            else {}
        )
        # 验收节点不参与信箱唤醒：唯一的激活源是绑定执行节点的待裁定提交
        # （派发扫描读握手）。其余消息——含 `wake_timer` 与模型消息——照常落箱，
        # 等下一次合法验收回合并入。被检验执行节点在唤醒上只放行裁决；用户/父级/
        # 前门的消息落箱不唤醒，其余系统消息排队。
        suppress_wake = (
            target_kind == 'acceptance'
            or (target_under_acceptance and str(system_kind or '').strip() != KIND_ACCEPTANCE_VERDICT)
        )

        accepted, reason = plan_delivery(target_park_state=park_state, sender_origin=origin)
        if not accepted:
            return {'target': normalized_node_id, 'status': 'rejected', 'reason': reason}

        kind = derive_message_kind(origin=origin, relation=relation, system_kind=system_kind)
        priority = derive_priority(kind=kind, verdict=verdict)
        body = str(message or '').strip()
        fingerprint = message_fingerprint(
            kind=kind, source_node_id=source_node_id, message=body, ref=ref
        )

        # 去重只对「还没被处理」的同一指纹生效：消费过的重复是一次新的意图。
        # 去重收敛的是消息本身，不是唤醒意图——重发一条仍在箱里的消息（例如重试
        # 一次 resume）如果只回 deduplicated 而不尝试唤醒，失败的唤醒就永远补不上。
        for item in list(self.store.list_task_node_notifications(normalized_task_id, normalized_node_id) or []):
            if (
                str(item.status or '').strip() == 'delivered'
                and str(item.fingerprint or '').strip() == fingerprint
            ):
                wake_outcome = ''
                if (
                    wake
                    and not notification_is_held(item)
                    and not suppress_wake
                    and self._delivery_wakes_target(
                        task_id=normalized_task_id,
                        node_id=normalized_node_id,
                        node=node,
                        priority=priority,
                        park_state=park_state,
                        sender_origin=origin,
                    )
                ):
                    try:
                        wake_outcome = await self.wake_node_on_message(
                            task_id=normalized_task_id, node_id=normalized_node_id
                        )
                    except Exception as exc:  # noqa: BLE001 - 消息已在箱里，唤醒失败不回滚
                        wake_outcome = f'wake_error:{type(exc).__name__}'
                return {
                    'target': normalized_node_id,
                    'status': 'deduplicated',
                    'message_id': str(item.notification_id or ''),
                    'kind': kind,
                    'priority': priority,
                    'wake': wake_outcome,
                    'reason': '',
                    **under_acceptance_fields,
                }

        notification = self.store.upsert_task_node_notification(
            TaskNodeNotification(
                notification_id=new_command_id().replace('command:', 'notif:', 1),
                task_id=normalized_task_id,
                node_id=normalized_node_id,
                epoch_id=str(epoch_id or '').strip(),
                source_node_id=str(source_node_id or '').strip(),
                message=body,
                status='delivered',
                created_at=now_iso(),
                delivered_at=now_iso(),
                consumed_at='',
                kind=kind,
                priority=priority,
                fingerprint=fingerprint,
                ref=str(ref or '').strip(),
                submission_id=str(submission_id or '').strip(),
                verdict=str(verdict or '').strip(),
                batch_id=str(batch_id or '').strip(),
                held_at=now_iso() if held else '',
                released_at='',
                payload={},
            )
        )
        self.log_service.refresh_task_view(normalized_task_id, mark_unread=True)

        wake_outcome = ''
        if wake and not held and not suppress_wake and self._delivery_wakes_target(
            task_id=normalized_task_id,
            node_id=normalized_node_id,
            node=node,
            priority=priority,
            park_state=park_state,
            sender_origin=origin,
        ):
            try:
                wake_outcome = await self.wake_node_on_message(
                    task_id=normalized_task_id, node_id=normalized_node_id
                )
            except Exception as exc:  # noqa: BLE001 - 投递已落箱，唤醒失败不能回滚它
                # 消息已经在信箱里；把调用打成失败会让发送方以为没送达并重发。
                # 唤醒失败只降级为一条读数，由对账/下次投递再唤醒。
                wake_outcome = f'wake_error:{type(exc).__name__}'
                try:
                    logger.warning(
                        'node wake failed after delivery: task={} node={} error={!r}',
                        normalized_task_id,
                        normalized_node_id,
                        exc,
                    )
                except Exception:
                    pass
        return {
            'target': normalized_node_id,
            'status': 'delivered',
            'message_id': str(notification.notification_id or ''),
            'kind': kind,
            'priority': priority,
            'wake': wake_outcome,
            'reason': '',
            **under_acceptance_fields,
        }

    async def deliver_control_node_message(self, **payload: Any) -> dict[str, Any]:
        """控制面投递：web 角色把写箱与唤醒交给 worker，绝不本地起 task actor。

        web 进程与 worker 各有一个 `MainRuntimeService`，但只有 worker 拥有任务执行体。
        在 web 进程本地写通知并唤醒，会同时拉起一个 web 侧 task actor；它的 dispatcher
        看不到 worker 正在跑的子节点，孤儿收尸器会把健康子节点暂停（2026-10-25 实盘）。
        因此 web 角色统一走 `send_node_message` 命令，由 worker 执行同一次
        `deliver_node_message`，回执原样带回。
        """
        if self.execution_mode != 'web':
            return await self.deliver_node_message(**payload)
        normalized_task_id = self.normalize_task_id(str(payload.get('task_id') or '').strip())
        target_node_id = self.normalize_node_id(str(payload.get('target_node_id') or '').strip())
        if not target_node_id:
            return {'target': target_node_id, 'status': 'rejected', 'reason': 'target_missing'}
        try:
            self._assert_worker_available()
        except ValueError as exc:
            return {'target': target_node_id, 'status': 'rejected', 'reason': str(exc)}
        task = self.get_task(normalized_task_id) if normalized_task_id else None
        session_id = self._task_origin_session_id(task)
        command_payload = {
            key: payload[key]
            for key in _WORKER_NODE_MESSAGE_FIELDS
            if key in payload
        }
        command_payload['task_id'] = normalized_task_id
        command_payload['target_node_id'] = target_node_id
        command_id = self._enqueue_task_command(
            command_type='send_node_message',
            task_id=normalized_task_id or None,
            session_id=session_id,
            payload=command_payload,
        )
        deadline = time.monotonic() + _WORKER_NODE_MESSAGE_TIMEOUT_SECONDS
        while True:
            current = self.store.get_task_command(command_id)
            status = str((current or {}).get('status') or '').strip().lower()
            if status in {'completed', 'failed'}:
                if status == 'failed':
                    return {
                        'target': target_node_id,
                        'status': 'rejected',
                        'reason': str((current or {}).get('error_text') or 'worker_delivery_failed'),
                        'worker_command_id': command_id,
                    }
                result = (
                    dict((current or {}).get('result') or {})
                    if isinstance((current or {}).get('result'), dict)
                    else {}
                )
                result.setdefault('target', target_node_id)
                result.setdefault('status', 'delivered')
                result['worker_command_id'] = command_id
                return result
            if time.monotonic() >= deadline:
                return {
                    'target': target_node_id,
                    'status': 'queued',
                    'reason': 'worker_delivery_pending',
                    'worker_command_id': command_id,
                }
            await asyncio.sleep(_WORKER_NODE_MESSAGE_POLL_SECONDS)

    async def _deliver_structural_node_message(self, **payload: Any) -> dict[str, Any]:
        """NodeRunner 的结构性消息投递口（交接单 / 验收裁决）。

        只是 `deliver_node_message` 的一跳间接：绑定的是这个方法而不是 bound
        method 本身，保证运行期替换/包装 `deliver_node_message` 时投递口跟着走，
        也让"验收消息确实走了同一条投递"在测试里可观测。
        """
        return await self.deliver_node_message(**payload)

    async def _deliver_node_message_from_node(
        self,
        *,
        task_id: str,
        sender_node_id: str,
        sender_is_root: bool,
        to: Any,
        content: str = '',
        ref: str = '',
    ) -> dict[str, Any]:
        """`send_message` 的寻址层：把 `parent` / `frontdoor` / 节点 id 解析成投递。

        越址不抛异常，而是在回执里给出合法目标集合——只告诉模型"不允许"会让它
        再猜一轮；告诉它"可以发给谁"只需要一次往返。
        """
        normalized_task_id = self.normalize_task_id(task_id)
        sender = self.store.get_node(sender_node_id)
        if sender is None:
            return {'ok': False, 'error': 'sender_missing', 'results': [], 'legal_targets': []}

        records = list(self.store.list_nodes(normalized_task_id) or [])
        node_map = {str(item.node_id or '').strip(): item for item in records}
        legal_targets, legal_truncated = self._messageable_targets(
            sender=sender,
            node_map=node_map,
            sender_is_root=sender_is_root,
        )

        requested = normalize_targets(to)
        if not requested:
            return {
                'ok': False,
                'error': 'no_targets',
                'results': [],
                'legal_targets': legal_targets,
                'legal_targets_truncated': legal_truncated,
            }

        results: list[dict[str, Any]] = []
        for requested_target in requested:
            target_id = requested_target
            if requested_target == 'parent':
                target_id = str(getattr(sender, 'parent_node_id', '') or '').strip()
                if not target_id:
                    results.append(
                        {'target': 'parent', 'status': 'rejected', 'reason': 'sender_has_no_parent'}
                    )
                    continue
            elif requested_target == 'frontdoor':
                if not sender_is_root:
                    results.append(
                        {
                            'target': 'frontdoor',
                            'status': 'rejected',
                            'reason': 'frontdoor_root_only',
                        }
                    )
                    continue
                results.append(
                    await self.deliver_frontdoor_message(
                        task_id=normalized_task_id,
                        source_node_id=str(sender.node_id or '').strip(),
                        content=content,
                        ref=ref,
                    )
                )
                continue

            target = node_map.get(str(target_id or '').strip())
            if target is None:
                reason = unresolved_target_reason(requested_target)
                rejection: dict[str, Any] = {
                    'target': requested_target,
                    'status': 'rejected',
                    'reason': reason,
                }
                # 形状档只在这一档给：地址档已经有 `legal_targets` 说清谁能收，再叠一句
                # 就是两条互相盖过的路径。
                if reason == REJECT_TARGET_LIST_UNPARSED:
                    rejection['hint'] = TARGET_LIST_UNPARSED_HINT
                results.append(rejection)
                continue
            relation, reject_reason = self._node_message_relation(
                sender=sender,
                target=target,
                node_map=node_map,
            )
            if reject_reason:
                results.append(
                    {
                        'target': requested_target,
                        'status': 'rejected',
                        'reason': reject_reason,
                        'sender_node_id': sender_node_id,
                    }
                )
                continue
            results.append(
                await self.deliver_node_message(
                    task_id=normalized_task_id,
                    target_node_id=target.node_id,
                    message=content,
                    origin='node',
                    relation=relation,
                    source_node_id=sender_node_id,
                    ref=ref,
                )
            )

        return {
            'ok': bool(results) and all(item.get('status') != 'rejected' for item in results),
            'results': results,
            'legal_targets': legal_targets,
            'legal_targets_truncated': legal_truncated,
        }

    @staticmethod
    def _is_descendant_node(target: Any, ancestor_id: str, node_map: dict[str, Any]) -> bool:
        return node_is_descendant(target, ancestor_id, node_map)

    def _node_message_relation(
        self,
        *,
        sender: Any,
        target: Any,
        node_map: dict[str, Any],
    ) -> tuple[str, str]:
        return node_message_relation(sender=sender, target=target, node_map=node_map)

    def _messageable_targets(
        self,
        *,
        sender: Any,
        node_map: dict[str, Any],
        sender_is_root: bool,
    ) -> tuple[list[str], bool]:
        return node_messageable_target_ids(
            sender=sender,
            node_map=node_map,
            sender_is_root=sender_is_root,
        )

    async def deliver_frontdoor_message(
        self,
        *,
        task_id: str,
        source_node_id: str,
        content: str,
        ref: str = '',
    ) -> dict[str, Any]:
        """Root node → frontdoor/session inbox (durable, signed)."""
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.store.get_task(normalized_task_id)
        node = self.store.get_node(str(source_node_id or '').strip())
        if task is None or node is None:
            return {'target': 'frontdoor', 'status': 'rejected', 'reason': 'source_missing'}
        if str(getattr(node, 'node_id', '') or '').strip() != str(getattr(task, 'root_node_id', '') or '').strip():
            return {'target': 'frontdoor', 'status': 'rejected', 'reason': 'frontdoor_root_only'}
        body = str(content or '').strip()
        if not body:
            return {'target': 'frontdoor', 'status': 'rejected', 'reason': 'content_required'}
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        message_id = new_command_id().replace('command:', 'fdmsg:', 1)
        record = FrontdoorNodeMessage(
            message_id=message_id,
            session_id=session_id,
            task_id=normalized_task_id,
            source_node_id=str(node.node_id or '').strip(),
            content=body,
            ref=str(ref or '').strip(),
            status='pending',
            created_at=now_iso(),
        )
        self.store.upsert_frontdoor_node_message(record)
        return {
            'target': 'frontdoor',
            'status': 'delivered',
            'message_id': message_id,
            'kind': 'frontdoor_notice',
            'priority': PRIORITY_HIGH,
            'wake': 'session_inbox',
            'reason': '',
        }

    async def deliver_frontdoor_node_message(
        self,
        *,
        session_id: str,
        to: Any,
        content: str,
        ref: str = '',
    ) -> dict[str, Any]:
        """Frontdoor → execution nodes in tasks owned by this session."""
        normalized_session = str(session_id or '').strip()
        body = str(content or '').strip()
        if not normalized_session:
            return {'ok': False, 'error': 'session_required', 'results': [], 'legal_targets': []}
        if not body:
            return {'ok': False, 'error': 'content_required', 'results': [], 'legal_targets': []}
        requested = normalize_targets(to)
        if not requested:
            return {'ok': False, 'error': 'no_targets', 'results': [], 'legal_targets': []}
        results: list[dict[str, Any]] = []
        for target in requested:
            node = self.store.get_node(str(target or '').strip())
            if node is None:
                results.append({'target': target, 'status': 'rejected', 'reason': 'target_missing'})
                continue
            task = self.store.get_task(str(getattr(node, 'task_id', '') or '').strip())
            if task is None or str(getattr(task, 'session_id', '') or '').strip() != normalized_session:
                results.append(
                    {'target': target, 'status': 'rejected', 'reason': 'not_session_task'}
                )
                continue
            if str(getattr(node, 'node_kind', '') or '').strip().lower() == 'acceptance':
                results.append(
                    {
                        'target': target,
                        'status': 'rejected',
                        'reason': 'acceptance_node_not_messageable',
                    }
                )
                continue
            results.append(
                await self.deliver_control_node_message(
                    task_id=str(getattr(node, 'task_id', '') or '').strip(),
                    target_node_id=str(node.node_id or '').strip(),
                    message=body,
                    origin='frontdoor',
                    ref=ref,
                )
            )
        return {
            'ok': bool(results) and all(item.get('status') != 'rejected' for item in results),
            'results': results,
            'legal_targets': [],
        }

    def list_frontdoor_node_messages(self, session_id: str) -> list[FrontdoorNodeMessage]:
        return list(self.store.list_frontdoor_node_messages(str(session_id or '').strip()) or [])

    def mark_frontdoor_node_messages_consumed(self, message_ids: list[str]) -> None:
        consumed_at = now_iso()
        for message_id in list(message_ids or []):
            normalized = str(message_id or '').strip()
            if not normalized:
                continue
            try:
                self.store.mark_frontdoor_node_message_consumed(
                    normalized, consumed_at=consumed_at
                )
            except Exception:
                continue

    async def _deliver_child_final_message(self, *, task_id: str, node_id: str) -> dict[str, Any]:
        """子节点终局 → 父节点信箱。内容取自主节点自己提交的结果载荷。

        这是运行时生成的系统消息（`system_kind=child_final`），模型无法通过
        `send_message` 伪造。父节点若在停靠，投递路径按 R2 唤醒它；若在跑，消息
        在下一个安全边界并入当前回合。
        """
        normalized_task_id = self.normalize_task_id(task_id)
        node = self.store.get_node(str(node_id or '').strip())
        if node is None:
            return {'status': 'rejected', 'reason': 'source_missing'}
        parent_node_id = str(getattr(node, 'parent_node_id', '') or '').strip()
        if not parent_node_id:
            return {'status': 'skipped', 'reason': 'no_parent'}
        status = str(getattr(node, 'status', '') or '').strip().lower()
        if status not in {'success', 'failed'}:
            return {'status': 'skipped', 'reason': 'source_not_terminal'}
        payload = normalize_result_payload((node.metadata or {}).get('result_payload'))
        summary = ''
        if payload is not None:
            summary = str(payload.summary or payload.answer or '').strip()
        summary = summary or str(getattr(node, 'final_output', '') or '').strip()
        summary = summary or str(getattr(node, 'failure_reason', '') or '').strip()
        summary = summary or ('执行完成' if status == 'success' else '执行失败')
        ref = str(getattr(node, 'final_output_ref', '') or '').strip()
        if not ref and isinstance(getattr(node, 'metadata', None), dict):
            ref = str((node.metadata or {}).get('result_payload_ref') or '').strip()
        goal = str(getattr(node, 'goal', '') or '').strip()
        verdict_word = '完成' if status == 'success' else '失败'
        body = f'子节点「{goal or node.node_id}」已{verdict_word}：{summary[:1200]}'
        wait_all, batch_id = self._spawn_batch_wait_policy(node=node)
        delivered = await self.deliver_node_message(
            task_id=normalized_task_id,
            target_node_id=parent_node_id,
            message=body,
            origin='node',
            # 子节点的终局消息：发送方是 child（relation 只是语义对齐，kind 由 system_kind 决定）。
            relation=RELATION_CHILD,
            system_kind=KIND_CHILD_FINAL,
            source_node_id=str(node.node_id or '').strip(),
            ref=ref,
            held=wait_all,
            batch_id=batch_id,
        )
        if wait_all and str(delivered.get('status') or '') in {'delivered', 'deduplicated'}:
            await self._release_wait_all_batch_if_settled(
                task_id=normalized_task_id,
                parent_node_id=parent_node_id,
                batch_id=batch_id,
            )
        return delivered

    def _spawn_batch_wait_policy(self, *, node: Any) -> tuple[bool, str]:
        """Return `(wait_all_children, batch_id)` for the child's spawn round."""
        parent_node_id = str(getattr(node, 'parent_node_id', '') or '').strip()
        metadata = getattr(node, 'metadata', None)
        round_id = str((metadata or {}).get('spawn_owner_round_id') or '').strip() if isinstance(metadata, dict) else ''
        if not parent_node_id or not round_id:
            return False, round_id
        parent = self.store.get_node(parent_node_id)
        parent_metadata = getattr(parent, 'metadata', None) if parent is not None else None
        operations = (parent_metadata or {}).get('spawn_operations') if isinstance(parent_metadata, dict) else {}
        payload = operations.get(round_id) if isinstance(operations, dict) else None
        return bool((payload or {}).get('wait_all_children')), round_id

    async def _release_wait_all_batch_if_settled(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        batch_id: str,
    ) -> bool:
        release = getattr(self.node_runner, 'release_wait_all_batch_if_settled', None)
        if not callable(release):
            return False
        released = bool(
            release(
                task_id=task_id,
                parent_node_id=parent_node_id,
                batch_id=batch_id,
            )
        )
        if released:
            parent = self.store.get_node(parent_node_id)
            handshake = normalize_acceptance_handshake(
                ((parent.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
                 if parent is not None and isinstance(parent.metadata, dict) else {})
            )
            if str(handshake.get('state') or '').strip() == ACCEPTANCE_STATE_WAITING_ACCEPTANCE:
                # 被检验节点只放行裁决；释放的行排队，等裁决把它带回执行态。
                return released
            try:
                await self.wake_node_on_message(task_id=task_id, node_id=parent_node_id)
            except Exception as exc:  # noqa: BLE001 - 行已释放，唤醒失败由调度扫描兜底
                logger.warning(
                    'wait_all batch release wake failed: task={} node={} error={!r}',
                    task_id,
                    parent_node_id,
                    exc,
                )
        return released


    async def _notify_wait_cycle(self, *, task_id: str, node_id: str, reason: str) -> dict[str, Any]:
        """等待图环检测的系统通知：以 frontdoor 身份投递（必唤醒，可外部破环）。"""
        return await self.deliver_node_message(
            task_id=task_id,
            target_node_id=node_id,
            message=str(reason or '').strip(),
            origin='frontdoor',
        )

    async def _reconcile_wait_all_batches(self) -> None:
        """Release held child_final rows whose batch settled while the process was down."""
        held_rows = list(self.store.list_held_task_node_notifications() or [])
        groups: dict[tuple[str, str, str], int] = {}
        for item in held_rows:
            key = (
                str(getattr(item, 'task_id', '') or '').strip(),
                str(getattr(item, 'node_id', '') or '').strip(),
                str(getattr(item, 'batch_id', '') or '').strip(),
            )
            if all(key):
                groups[key] = groups.get(key, 0) + 1
        for (task_id, parent_node_id, batch_id), count in groups.items():
            released = await self._release_wait_all_batch_if_settled(
                task_id=task_id,
                parent_node_id=parent_node_id,
                batch_id=batch_id,
            )
            if not released:
                logger.warning(
                    'held child_final batch not releasable at startup: task={} parent={} batch={} rows={}',
                    task_id,
                    parent_node_id,
                    batch_id,
                    count,
                )

    def _subtree_node_ids(self, task_id: str, root_node_id: str) -> list[str]:
        records = list(self.store.list_nodes(str(task_id or '').strip()) or [])
        children_by_parent: dict[str, list[str]] = {}
        depths: dict[str, int] = {}
        for item in records:
            node_id = str(getattr(item, 'node_id', '') or '').strip()
            if not node_id:
                continue
            depths[node_id] = int(getattr(item, 'depth', 0) or 0)
            parent_id = str(getattr(item, 'parent_node_id', '') or '').strip()
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(node_id)
        root_id = str(root_node_id or '').strip()
        ordered: list[str] = []
        queue = [root_id] if root_id else []
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            queue.extend(children_by_parent.get(current) or [])
        ordered.sort(key=lambda item: (depths.get(item, 0), item))
        return ordered

    async def _terminate_subtree(
        self,
        *,
        task_id: str,
        root_node_id: str,
        reason: str,
    ) -> list[str]:
        subtree_ids = self._subtree_node_ids(task_id, root_node_id)
        owned: set[str] = set()
        dispatcher = self.task_actor_service._dispatchers.get(task_id)
        if dispatcher is not None and subtree_ids:
            try:
                owned = set(await dispatcher.cancel_nodes(subtree_ids, reason))
            except Exception as exc:  # noqa: BLE001 - 取消失败仍要把行落定，避免孤儿
                logger.warning(
                    'subtree terminate cancel failed: task={} root={} error={!r}',
                    task_id,
                    root_node_id,
                    exc,
                )
        terminated: list[str] = []
        for node_id in subtree_ids:
            if node_id in owned:
                terminated.append(node_id)
                continue
            node = self.store.get_node(node_id)
            if node is None:
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() in {'success', 'failed'}:
                continue
            try:
                self.node_runner.fail_paused_node(
                    task_id,
                    node_id,
                    reason,
                    notify_parent=False,
                )
                terminated.append(node_id)
            except Exception as exc:  # noqa: BLE001 - 单行失败不阻断其余子树
                logger.warning(
                    'subtree terminate fail_node failed: task={} node={} error={!r}',
                    task_id,
                    node_id,
                    exc,
                )
        return terminated

    async def _deliver_child_abnormal_pause(
        self,
        *,
        task_id: str,
        node_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """子节点异常暂停 → 父节点信箱（runtime 生成，模型无法伪造）。"""
        normalized_task_id = self.normalize_task_id(task_id)
        node = self.store.get_node(str(node_id or '').strip())
        if node is None:
            return {'status': 'rejected', 'reason': 'source_missing'}
        parent_node_id = str(getattr(node, 'parent_node_id', '') or '').strip()
        if not parent_node_id:
            return {'status': 'skipped', 'reason': 'no_parent'}
        goal = str(getattr(node, 'goal', '') or '').strip() or str(node.node_id or '')
        reason_text = str(reason or '').strip() or '节点被判定为无进展并暂停'
        parent = self.store.get_node(parent_node_id)
        receiver_is_root = bool(parent is not None) and not str(
            getattr(parent, 'parent_node_id', '') or ''
        ).strip()
        body = (
            f'子节点「{goal}」已被暂停（异常暂停）。\n'
            f'原因：{reason_text[:1200]}\n'
            f'节点 id：{node.node_id}。请决定是继续推进、改写要求还是终止这条分支。\n'
            f'{error_escalation_hint(receiver_is_root=receiver_is_root)}'
        )
        return await self.deliver_node_message(
            task_id=normalized_task_id,
            target_node_id=parent_node_id,
            message=body,
            origin='node',
            # 发送方是 child（relation 只做语义对齐，kind 由 system_kind 决定）。
            relation=RELATION_CHILD,
            system_kind=KIND_CHILD_ABNORMAL_PAUSE,
            source_node_id=str(node.node_id or '').strip(),
        )

    async def resend_lost_abnormal_pause_escalations(self, task_id: str) -> list[str]:
        """P8：异常暂停的升级消息丢了就补发（防 P6 第一类死锁从崩溃路径复活）。

        判据只看持久事实：节点是 `pause_reason='error'` 的暂停态、有父节点，而父
        节点信箱里没有来自它的 `child_abnormal_pause`（delivered/consumed 都算已到）。
        """
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return []
        resent: list[str] = []
        for node in list(self.store.list_nodes(normalized_task_id) or []):
            if str(getattr(node, 'node_kind', '') or '').strip().lower() != 'execution':
                continue
            if str(getattr(node, 'pause_reason', '') or '').strip().lower() != 'error':
                continue
            if not (bool(getattr(node, 'is_paused', False)) or bool(getattr(node, 'pause_requested', False))):
                continue
            parent_node_id = str(getattr(node, 'parent_node_id', '') or '').strip()
            if not parent_node_id:
                continue
            already = any(
                str(item.kind or '').strip() == KIND_CHILD_ABNORMAL_PAUSE
                and str(item.source_node_id or '').strip() == str(node.node_id or '').strip()
                and str(item.status or '').strip() in {'delivered', 'consumed'}
                for item in list(self.store.list_task_node_notifications(normalized_task_id, parent_node_id) or [])
            )
            if already:
                continue
            await self._deliver_child_abnormal_pause(
                task_id=normalized_task_id,
                node_id=str(node.node_id or '').strip(),
                reason=str(getattr(node, 'failure_reason', '') or '异常暂停'),
            )
            resent.append(str(node.node_id or '').strip())
        return resent

    async def _manage_child_nodes_from_node(
        self,
        *,
        task_id: str,
        sender_node_id: str,
        ops: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """`manage_child_nodes` 的裁定层：批量 pause / resume / terminate，只限直接子节点。

        `resume` 按设计 §2.3 的"唯一激活通道"实现——不是清标志，而是**代父节点写一条
        `parent_notice`**（正文 = `note`），由投递路径按 R2 唤醒收件人；恢复与修复信息
        一次完成。暂停态的投递矩阵本就接收机器消息，`wake_parked_node` 会在唤醒前清掉
        暂停位。`note` 为空时给一句最小正文，避免"空消息唤醒"让模型无从判断为什么醒。

        `terminate` 走"先挂暂停位、再落失败"两步，而不是放开
        `_apply_fail_node_command` 的"必须已暂停"前置。理由：正在跑的回合只在
        `_check_pause_or_cancel` 检查点读暂停位，不读节点行终态；先直接落 `failed`
        会让协程在下一个检查点之前继续烧模型调用与工具预算，而"任意非终态 → 失败"
        的设计语义要求终止立即生效。两步法把影响面限制在本工具内，不改 CEO /
        worker 命令通道沿用多年的前置条件（那些通道的暂停语义有自己的事故史）。
        终止由父节点自己发起，按设计 §4.4 不向父节点重复投递 `child_final`。
        """
        normalized_task_id = self.normalize_task_id(task_id)
        sender = self.store.get_node(str(sender_node_id or '').strip())
        if sender is None or str(sender.task_id or '').strip() != normalized_task_id:
            return {'ok': False, 'error': 'sender_missing', 'items': [], 'legal_targets': []}

        children = {
            str(child.node_id or '').strip(): child
            for child in list(self.store.list_nodes(normalized_task_id) or [])
            if str(getattr(child, 'parent_node_id', '') or '').strip() == str(sender.node_id or '').strip()
        }
        legal_targets = [
            node_id
            for node_id, child in children.items()
            if str(getattr(child, 'node_kind', '') or '').strip().lower() != 'acceptance'
        ]
        items: list[dict[str, Any]] = []

        for raw in list(ops or []):
            item = dict(raw) if isinstance(raw, dict) else {}
            node_id = str(item.get('node_id') or '').strip()
            action = str(item.get('action') or '').strip().lower()
            note = str(item.get('note') or '').strip()
            reason = str(item.get('reason') or '').strip()

            child = children.get(node_id)
            if child is None:
                items.append(
                    {'node_id': node_id, 'action': action, 'result': 'rejected', 'reason': 'not_a_direct_child'}
                )
                continue
            if action not in {'pause', 'resume', 'terminate'}:
                items.append(
                    {'node_id': node_id, 'action': action, 'result': 'rejected', 'reason': 'invalid_action'}
                )
                continue
            if str(getattr(child, 'node_kind', '') or '').strip().lower() == 'acceptance':
                items.append(
                    {
                        'node_id': node_id,
                        'action': action,
                        'result': 'rejected',
                        'reason': 'acceptance_node_not_controllable',
                    }
                )
                continue
            if str(child.status or '').strip().lower() in {'success', 'failed'}:
                items.append(
                    {'node_id': node_id, 'action': action, 'result': 'rejected', 'reason': 'node_terminal'}
                )
                continue
            paused = bool(getattr(child, 'is_paused', False)) or bool(getattr(child, 'pause_requested', False))

            if action == 'pause':
                if paused:
                    items.append({'node_id': node_id, 'action': action, 'result': 'paused', 'reason': 'already_paused'})
                    continue
                await self._apply_pause_node_command(
                    normalized_task_id,
                    node_ids=[node_id],
                    reason='agent',
                    remark=note,
                )
                items.append({'node_id': node_id, 'action': action, 'result': 'paused'})
                continue

            if action == 'resume':
                if not paused:
                    phase = self._frame_phase(normalized_task_id, node_id)
                    if phase in {'waiting_explicit', 'waiting_acceptance'}:
                        items.append(
                            {
                                'node_id': node_id,
                                'action': action,
                                'result': 'rejected',
                                'reason': 'node_waiting',
                                'hint': (
                                    '该节点在等待唤醒源，消息到达即自动继续；resume 只对暂停态有效，'
                                    '如需放弃请用 terminate。'
                                ),
                            }
                        )
                        continue
                    items.append(
                        {'node_id': node_id, 'action': action, 'result': 'rejected', 'reason': 'node_not_paused'}
                    )
                    continue
                delivered = await self.deliver_node_message(
                    task_id=normalized_task_id,
                    target_node_id=node_id,
                    message=note or '上级节点恢复了你，请继续当前任务。',
                    origin='node',
                    # 我是发送方、也是收件人的父 ⇒ relation='parent' ⇒ parent_notice。
                    relation=RELATION_PARENT,
                    source_node_id=str(sender.node_id or '').strip(),
                )
                if str(delivered.get('status') or '') in {'delivered', 'deduplicated'}:
                    items.append(
                        {
                            'node_id': node_id,
                            'action': action,
                            'result': 'resumed',
                            'delivery': str(delivered.get('status') or ''),
                            'wake': str(delivered.get('wake') or ''),
                        }
                    )
                else:
                    items.append(
                        {
                            'node_id': node_id,
                            'action': action,
                            'result': 'rejected',
                            'reason': str(delivered.get('reason') or 'delivery_failed'),
                        }
                    )
                continue

            # terminate
            if not reason:
                # 工具层已同步打回整批；这里是服务层的第二道闸（worker 命令或内部
                # 调用绕开工具 schema 时同样拒绝，不留"无理由终止"的入口）。
                items.append(
                    {'node_id': node_id, 'action': action, 'result': 'rejected', 'reason': 'reason_required'}
                )
                continue
            # terminate: 目标子树整体终止（含验收节点）；终态行保留原结果。
            terminated_ids = await self._terminate_subtree(
                task_id=normalized_task_id,
                root_node_id=node_id,
                reason=reason,
            )
            items.append(
                {
                    'node_id': node_id,
                    'action': action,
                    'result': 'terminated',
                    'terminated_node_ids': terminated_ids,
                }
            )

        return {
            'ok': bool(items) and all(str(item.get('result') or '') != 'rejected' for item in items),
            'items': items,
            'legal_targets': legal_targets,
        }

    def list_active_task_snapshots_for_session(self, session_id: str, *, limit: int = 3) -> list[dict[str, str]]:
        unfinished = list(self.list_unfinished_tasks_for_session(session_id))
        unfinished.sort(
            key=lambda item: (
                str(getattr(item, 'updated_at', '') or ''),
                str(getattr(item, 'created_at', '') or ''),
                str(getattr(item, 'task_id', '') or ''),
            ),
            reverse=True,
        )
        snapshots: list[dict[str, str]] = []
        for task in unfinished[: max(1, int(limit or 3))]:
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            item = {
                'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                'title': str(getattr(task, 'title', '') or '').strip(),
                'core_requirement': str(metadata.get('core_requirement') or '').strip(),
                'status': str(getattr(task, 'status', '') or '').strip(),
                'updated_at': str(getattr(task, 'updated_at', '') or '').strip(),
            }
            snapshots.append({key: value for key, value in item.items() if value})
        return snapshots

    def get_session_task_counts(self, session_id: str) -> dict[str, int]:
        tasks = self.list_tasks_for_session(session_id)
        in_progress = 0
        paused = 0
        terminal = 0
        for task in tasks:
            status = str(getattr(task, 'status', '') or '').strip().lower()
            if status != 'in_progress':
                terminal += 1
            elif bool(getattr(task, 'is_paused', False)):
                paused += 1
            else:
                in_progress += 1
        return {
            'total': len(tasks),
            'unfinished': in_progress,
            'in_progress': in_progress,
            'paused': paused,
            'terminal': terminal,
            'deletable': terminal + paused,
        }

    async def delete_task_records_for_session(self, session_id: str) -> int:
        deleted = 0
        for task in list(self.list_tasks_for_session(session_id)):
            try:
                delete_helper = getattr(self, '_delete_task_with_task_delete_semantics', None)
                if callable(delete_helper):
                    removed = await delete_helper(task.task_id)
                else:
                    status = str(getattr(task, 'status', '') or '').strip().lower()
                    if status == 'in_progress' and not bool(getattr(task, 'is_paused', False)):
                        pause = getattr(self, 'pause_task', None)
                        if callable(pause):
                            await pause(task.task_id)
                    removed = await self.delete_task(task.task_id)
            except ValueError as exc:
                if str(exc) in {'task_not_paused', 'task_still_stopping'}:
                    continue
                raise
            if removed is not None:
                deleted += 1
        return deleted

    def get_node(self, node_id: str) -> NodeRecord | None:
        return self.store.get_node(node_id)

    def list_nodes(self, task_id: str) -> list[NodeRecord]:
        task_id = self.normalize_task_id(task_id)
        return self.store.list_nodes(task_id)

    def normalize_task_id(self, task_id: str) -> str:
        raw = str(task_id or '').strip()
        if not raw or raw.startswith('task:') or ':' in raw:
            return raw
        return f'task:{raw}'

    def normalize_node_id(self, node_id: str) -> str:
        raw = str(node_id or '').strip()
        if not raw or raw.startswith('node:') or ':' in raw:
            return raw
        return f'node:{raw}'

    def bind_resource_manager(self, resource_manager) -> None:
        self._resource_manager = resource_manager
        self.resource_registry.bind_resource_manager(resource_manager)
        self._record_resource_tree_state()

    def bind_runtime_loop(self, loop: Any | None) -> None:
        self._runtime_loop = loop

    @staticmethod
    def _stall_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec='microseconds')

    def get_task_user_node_supplements(self, task_id: str) -> list[dict[str, str]]:
        """任务期间用户对各节点追加的定向通知（终态心跳附带，会话模型对齐需求用）。"""
        return collect_task_user_node_supplements(str(task_id or '').strip(), store=self.store)

    def _reap_task_owned_processes(self, task_id: str) -> None:
        """任务终态回收该任务起的后台进程：`exec background` 的所有权随任务结束。"""
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return
        try:
            from g3ku.runtime import owned_processes

            killed = owned_processes.reap_scope(
                owned_processes.scope_key(session_key='', task_id=normalized_task_id)
            )
            if killed:
                logger.info(
                    'reaped task-owned background processes task={} pids={}',
                    normalized_task_id,
                    killed,
                )
        except Exception:
            logger.exception('failed to reap owned background processes for task {}', normalized_task_id)

    def _enqueue_task_terminal_callback(self, task: TaskRecord) -> None:
        if self.execution_mode != 'worker':
            return
        self._reap_task_owned_processes(str(getattr(task, 'task_id', '') or ''))
        payload = enrich_task_terminal_payload(
            build_task_terminal_payload(task),
            task=task,
            node_detail_getter=self.get_node_detail_payload,
            output_resolver=build_terminal_output_resolver(self.content_store),
            supplement_getter=self.get_task_user_node_supplements,
        )
        if not payload:
            return
        dedupe_key = str(payload.get('dedupe_key') or '').strip()
        if not dedupe_key:
            return
        created_at = str(payload.get('finished_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_terminal_outbox(
                dedupe_key=dedupe_key,
                task_id=str(payload.get('task_id') or '').strip(),
                session_id=str(payload.get('session_id') or '').strip() or 'web:shared',
                created_at=created_at,
                payload=payload,
            )
        except Exception:
            logger.exception('failed to persist task terminal outbox for {}', dedupe_key)
            return
        self._schedule_task_terminal_delivery(dedupe_key)

    def emit_task_stall(self, payload: dict[str, Any] | None) -> bool:
        normalized = normalize_task_stall_payload(payload)
        if not normalized:
            return False
        if self.execution_mode == 'worker':
            self._enqueue_task_stall_callback(normalized)
            return True
        loop = getattr(self, '_runtime_loop', None)
        heartbeat = getattr(loop, 'web_session_heartbeat', None) if loop is not None else None
        if heartbeat is None or not hasattr(heartbeat, 'enqueue_task_stall_payload'):
            return False
        return bool(heartbeat.enqueue_task_stall_payload(normalized))

    def emit_task_distribution_error(self, *, task_id: str, epoch_id: str, error_text: str) -> bool:
        task = self.get_task(task_id)
        payload = {
            "task_id": task_id,
            "session_id": self._task_origin_session_id(task),
            "title": str(getattr(task, "title", "") or task_id).strip() or task_id,
            "epoch_id": epoch_id,
            "error_text": error_text,
        }
        normalized = normalize_task_distribution_error_payload(payload)
        if not normalized:
            return False
        if self.execution_mode == 'worker':
            self._enqueue_task_distribution_error_callback(normalized)
            return True
        loop = getattr(self, '_runtime_loop', None)
        heartbeat = getattr(loop, 'web_session_heartbeat', None) if loop is not None else None
        if heartbeat is None or not hasattr(heartbeat, 'enqueue_task_distribution_error_payload'):
            return False
        return bool(heartbeat.enqueue_task_distribution_error_payload(normalized))

    def classify_task_stall_reason(
        self,
        task_id: str,
        *,
        runtime_state: dict[str, Any] | None = None,
    ) -> str:
        task = self.get_task(task_id)
        if task is None:
            return TASK_STALL_REASON_MISSING_TASK
        if str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
            return TASK_STALL_REASON_NOT_IN_PROGRESS
        current_runtime_state = runtime_state if isinstance(runtime_state, dict) else (
            self.log_service.read_runtime_state(task.task_id, frame_mode='stall') or {}
        )
        if bool(getattr(task, 'is_paused', False)) or bool(getattr(task, 'pause_requested', False)):
            return TASK_STALL_REASON_USER_PAUSED
        if bool(current_runtime_state.get('paused')) or bool(current_runtime_state.get('pause_requested')):
            return TASK_STALL_REASON_USER_PAUSED
        if bool(getattr(task, 'cancel_requested', False)):
            return TASK_STALL_REASON_CANCEL_REQUESTED
        if bool(current_runtime_state.get('cancel_requested')):
            return TASK_STALL_REASON_CANCEL_REQUESTED
        if self.execution_mode == 'web':
            if not self.is_worker_online():
                return TASK_STALL_REASON_WORKER_UNAVAILABLE
        return TASK_STALL_REASON_SUSPECTED_STALL

    def is_task_stall_actionable(
        self,
        task_id: str,
        *,
        runtime_state: dict[str, Any] | None = None,
    ) -> bool:
        return self.classify_task_stall_reason(
            task_id,
            runtime_state=runtime_state,
        ) == TASK_STALL_REASON_SUSPECTED_STALL

    def _build_task_stall_detail(
        self,
        task_id: str,
        *,
        runtime_state: dict[str, Any],
        runtime_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """停滞事件的**事实**部分：闸位、上游限流、谁占着容量、这一拍卡在哪个相位。

        纪律：这里只写能观测到的量，不写建议。会话拿到"闸 12/12、本任务排队 1、全局排队
        30、占位任务是 X"之后自己判断要不要动——历史上每次把动作写进事件正文，第二次就会
        出现"照建议做了反而更慢"的形态（暂停排队方不释放任何格子，恢复还要排队尾）。
        """
        meta = dict(runtime_meta if isinstance(runtime_meta, dict) else (self.log_service.read_task_runtime_meta(task_id) or {}))
        detail: dict[str, Any] = {}

        gate: dict[str, Any] = {}
        gate['task_running'] = {
            role: int(value or 0) for role, value in dict(meta.get('dispatch_running') or {}).items()
        }
        gate['task_queued'] = {
            role: int(value or 0) for role, value in dict(meta.get('dispatch_queued') or {}).items()
        }
        gate['task_limits'] = {
            role: int(value or 0) for role, value in dict(meta.get('dispatch_limits') or {}).items()
        }
        monitor_snapshot: dict[str, Any] = {}
        monitor = getattr(self, 'tool_pressure_monitor', None)
        if monitor is not None:
            try:
                monitor_snapshot = dict(monitor.snapshot() or {})
            except Exception:
                monitor_snapshot = {}
        if monitor_snapshot:
            pool = dict(monitor_snapshot.get('entry_gate_pool') or {})
            if pool:
                gate['pool_running'] = int(pool.get('running') or 0)
                gate['pool_limit'] = int(pool.get('limit') or 0)
                gate['pool_queued'] = int(pool.get('queued') or 0)
                gate['pool_ceiling'] = int(pool.get('ceiling') or 0)
            gate['reserved'] = {
                role: int(value or 0) for role, value in dict(monitor_snapshot.get('entry_gate_reserved') or {}).items()
            }
            gate['binding_seconds'] = round(float(monitor_snapshot.get('entry_binding_seconds') or 0.0), 1)
        if gate:
            detail['entry_gate'] = gate

        # 闸内那一层的排队。缺这块时，会话只看到 `dispatch_running=10` 就会判成
        # "10 个节点在推进"，而那 10 个其实全都卡在等模型回合权（2026-10-12 实测：
        # 09:20 与 09:28 两轮据此判"并非真死锁"，同期授予数恒 0、队列 running=0/waiting=13）。
        # 直接取控制器的现读快照：`monitor.snapshot()` 里没有这几项（它们由本服务合并进
        # 状态载荷与 perf 历史，不在压力采样器自己的字典里）。
        turn: dict[str, Any] = {}
        node_turn = self._node_turn_snapshot()
        if node_turn:
            turn['running'] = int(node_turn.get('node_queue_running_count') or 0)
            turn['waiting'] = int(node_turn.get('node_queue_waiting_count') or 0)
            oldest_ms = float(node_turn.get('node_queue_oldest_wait_ms') or 0.0)
            if oldest_ms > 0:
                turn['oldest_wait_seconds'] = round(oldest_ms / 1000.0)
            head_pinned = float(node_turn.get('node_queue_head_pinned_seconds') or 0.0)
            if head_pinned > 0:
                turn['head_pinned_seconds'] = round(head_pinned)
            head_reason = str(node_turn.get('node_queue_head_busy_reason') or '').strip()
            if head_reason:
                turn['head_busy_reason'] = head_reason
            head_node = str(node_turn.get('node_queue_head_node_id') or '').strip()
            if head_node:
                turn['head_node_id'] = head_node
            head_window = int(node_turn.get('node_queue_head_required_window') or 0)
            if head_window > 0:
                turn['head_required_window'] = head_window
        if turn:
            detail['turn_queue'] = turn

        rate: dict[str, Any] = {}
        if monitor_snapshot:
            rate['requests_per_minute'] = float(monitor_snapshot.get('entry_throughput_rpm_60s') or 0.0)
            rate['completions_per_minute'] = float(monitor_snapshot.get('entry_completions_60s') or 0.0)
            rate['penalty_429_max'] = float(monitor_snapshot.get('model_rate_penalty_429_max') or 0.0)
            rate['rolling_rpm_sum'] = int(monitor_snapshot.get('model_rolling_rpm_60s_sum') or 0)
            rate['local_pressure_state'] = str(monitor_snapshot.get('local_pressure_state') or '')
            rate['machine_pressure_state'] = str(monitor_snapshot.get('machine_pressure_state') or '')
            rate['memory_available_bytes'] = int(monitor_snapshot.get('machine_memory_available_bytes') or -1)
            rate['slot_memory_bytes'] = int(monitor_snapshot.get('entry_slot_memory_bytes') or 0)
        if rate:
            detail['upstream_rate'] = rate

        # 谁占着容量：本任务之外、仍在跑的任务各自的闸内/闸外条数。会话要判断"该不该让
        # 路"，缺的就是这份；只报"资源紧张"而不报谁在吃，等于把决定权交出去又不给依据。
        occupiers: list[dict[str, Any]] = []
        try:
            for other in list(self.store.list_tasks())[: _STALL_DETAIL_OCCUPIER_SCAN_LIMIT]:
                other_task_id = str(getattr(other, 'task_id', '') or '').strip()
                if not other_task_id or other_task_id == str(task_id or '').strip():
                    continue
                if str(getattr(other, 'status', '') or '').strip().lower() != 'in_progress':
                    continue
                other_meta = dict(self.log_service.read_task_runtime_meta(other_task_id) or {})
                other_running = sum(int(value or 0) for value in dict(other_meta.get('dispatch_running') or {}).values())
                other_queued = sum(int(value or 0) for value in dict(other_meta.get('dispatch_queued') or {}).values())
                if other_running <= 0 and other_queued <= 0:
                    continue
                occupiers.append(
                    {
                        'task_id': other_task_id,
                        'running': other_running,
                        'queued': other_queued,
                        'last_visible_output_at': str(other_meta.get('last_visible_output_at') or ''),
                    }
                )
                if len(occupiers) >= _STALL_DETAIL_OCCUPIER_LIMIT:
                    break
        except Exception:
            occupiers = []
        if occupiers:
            detail['occupiers'] = occupiers

        lane: dict[str, Any] = {}
        deadline = running_tool_deadline(runtime_state)
        if deadline is not None:
            lane['running_tool_deadline'] = deadline.isoformat(timespec='seconds')
        frames = list(runtime_state.get('frames') or []) if isinstance(runtime_state, dict) else []
        for frame in frames[: _STALL_DETAIL_FRAME_LIMIT]:
            if not isinstance(frame, dict):
                continue
            lane['frames'] = lane.get('frames') or []
            lane['frames'].append(
                {
                    'node_id': str(frame.get('node_id') or ''),
                    'phase': str(frame.get('phase') or ''),
                    'updated_at': str(frame.get('updated_at') or ''),
                }
            )
        if lane:
            detail['lane'] = lane
        return detail

    def build_task_stall_payload(
        self,
        task_id: str,
        *,
        bucket_minutes: int,
        last_visible_output_at: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            return {}
        origin_session_id = self._task_origin_session_id(task)
        if not task_notice_audience_ok(origin_session_id):
            return {}
        runtime_state = self.log_service.read_runtime_state(task.task_id, frame_mode='stall') or {}
        stall_reason = self.classify_task_stall_reason(task.task_id, runtime_state=runtime_state)
        if stall_reason != TASK_STALL_REASON_SUSPECTED_STALL:
            return {}
        visible_at = str(runtime_state.get('last_visible_output_at') or task.created_at or '').strip()
        minute_seconds = float(getattr(self.task_stall_notifier, 'minute_seconds', 60.0) or 60.0)
        # 静默锚点与失速判定保持一致：运行中工具的统一超时截止时间晚于最近可见输出时，
        # 以截止时间计失速时长，工具正常运行期间不计入（详见 heartbeat-system.md「Task Stall Detection」）。
        silence_start = effective_silence_start(runtime_state, visible_at)
        baseline_iso = silence_start.isoformat() if silence_start is not None else visible_at
        current_bucket = stall_bucket_minutes(baseline_iso, minute_seconds=minute_seconds)
        if current_bucket <= 0:
            return {}
        # 时长必须由事件里印出的那个锚点算出。旧写法把调用方上一拍算好的 bucket 与本拍
        # 重读的锚点取 max，屏障解除的那一秒两份读数跨在复位写的前后，于是印出
        # 「最后可见输出＝此刻」＋「已静默 30 分钟」（实盘 task:edf015cc47dc，03:08:33 同秒两档）。
        stalled_minutes = stalled_minutes_since(baseline_iso, minute_seconds=minute_seconds)
        detail = self.get_task_detail_payload(task.task_id, mark_read=False) or {}
        paused_nodes = [
            {
                'node_id': str(item.node_id or '').strip(),
                'pause_reason': str(item.pause_reason or '').strip(),
                'remark': str(item.remark or '').strip(),
            }
            for item in list(self.log_service.list_node_pauses(task.task_id) or [])
            if str(item.node_id or '').strip()
        ]
        return normalize_task_stall_payload(
            {
                'task_id': task.task_id,
                'session_id': origin_session_id,
                'title': str(getattr(task, 'title', '') or task.task_id).strip() or task.task_id,
                'reason': stall_reason,
                'stalled_minutes': stalled_minutes,
                'bucket_minutes': current_bucket,
                'last_visible_output_at': baseline_iso,
                'brief_text': str(getattr(task, 'brief_text', '') or '').strip(),
                'latest_node_summary': self._task_stall_latest_node_summary(detail),
                'runtime_summary_excerpt': self._task_stall_runtime_summary(detail),
                'perf_window_summary': self._perf_stall_window_summary(baseline_iso),
                'paused_nodes': paused_nodes,
                'stall_detail': self._build_task_stall_detail(task.task_id, runtime_state=runtime_state),
            }
        )

    def _task_stall_latest_node_summary(self, detail: dict[str, Any]) -> str:
        payload = detail if isinstance(detail, dict) else {}
        task_id = str((payload.get('task') or {}).get('task_id') or '').strip()
        root_node = payload.get('root_node') if isinstance(payload.get('root_node'), dict) else {}
        frontier = [item for item in list(payload.get('frontier') or []) if isinstance(item, dict)]
        candidate = root_node
        frontier_node_id = str(frontier[0].get('node_id') or '').strip() if frontier else ''
        root_node_id = str(root_node.get('node_id') or '').strip()
        if task_id and frontier_node_id and frontier_node_id != root_node_id:
            detail_payload = self.get_node_detail_payload(task_id, frontier_node_id) or {}
            candidate = detail_payload.get('item') if isinstance(detail_payload.get('item'), dict) else candidate
        if not candidate:
            return ''
        title = str(self._repair_legacy_display_text(candidate.get('goal') or candidate.get('title') or candidate.get('node_id') or 'node')).strip() or 'node'
        status = str(candidate.get('status') or 'in_progress').strip() or 'in_progress'
        output = str(self._repair_legacy_display_text(
            candidate.get('final_output')
            or candidate.get('output')
            or candidate.get('failure_reason')
            or ''
        )).strip()
        if not output and frontier:
            output = str(self._repair_legacy_display_text(frontier[0].get('phase') or '')).strip()
        text = f'{title} [{status}]'
        if output:
            text = f'{text}: {output}'
        return text[:240]

    def _task_stall_runtime_summary(self, detail: dict[str, Any]) -> str:
        payload = detail if isinstance(detail, dict) else {}
        frames = [item for item in list(payload.get('frontier') or []) if isinstance(item, dict)]
        parts: list[str] = []
        for frame in frames[:3]:
            node_id = str(frame.get('node_id') or '').strip() or 'node'
            phase = str(frame.get('phase') or '').strip() or 'waiting'
            tool_calls = [item for item in list(frame.get('tool_calls') or []) if isinstance(item, dict)]
            child_pipelines = [item for item in list(frame.get('child_pipelines') or []) if isinstance(item, dict)]
            running_tools = sum(
                1
                for item in tool_calls
                if str(item.get('status') or '').strip().lower() in {'queued', 'running'}
            )
            running_children = sum(
                1
                for item in child_pipelines
                if str(item.get('status') or '').strip().lower() in {'queued', 'running'}
            )
            summary = f'{node_id} phase={phase}'
            if tool_calls:
                summary = f'{summary} tools={running_tools}/{len(tool_calls)}'
            if child_pipelines:
                summary = f'{summary} children_running={running_children}/{len(child_pipelines)}'
            parts.append(summary)
        return '; '.join(parts)[:320]

    def _perf_stall_window_summary(self, baseline_iso: str) -> str:
        """静默锚点到现在的性能判读，一行；与 perf_inspect 工具同一份聚合口径。"""
        anchor = self._perf_sample_time(baseline_iso)
        if anchor is None:
            return ''
        return self._perf_window_brief(start_at=anchor, end_at=datetime.now(timezone.utc))[:400]

    def _enqueue_task_worker_status_callback(self, payload: dict[str, Any] | None) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(
            {
                'event_type': 'task.worker.status',
                'session_id': 'all',
                'data': dict(payload or {}),
            }
        )
        if not normalized:
            return
        data = dict(normalized.get('data') or {})
        worker = dict(data.get('worker') or {}) if isinstance(data.get('worker'), dict) else {}
        worker_id = str(worker.get('worker_id') or '').strip()
        if not worker_id:
            return
        created_at = str(worker.get('updated_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_worker_status_outbox(
                worker_id=worker_id,
                created_at=created_at,
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist worker status outbox for {}', worker_id)
            return
        self._schedule_task_worker_status_delivery(worker_id)

    def _summary_stats_snapshot(self) -> dict[str, float]:
        snapshot = {key: float(value or 0.0) for key, value in dict(self._task_summary_stats or {}).items()}
        snapshot['task_summary_pending_count'] = float(len(self._pending_task_summaries))
        return snapshot

    def _increment_summary_stat(self, key: str, amount: float = 1.0) -> None:
        normalized_key = str(key or '').strip()
        if not normalized_key:
            return
        current = float(self._task_summary_stats.get(normalized_key, 0.0) or 0.0)
        self._task_summary_stats[normalized_key] = current + float(amount or 0.0)

    @staticmethod
    def _summary_payload_task_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
        data = dict((payload or {}).get('data') or {})
        return dict(data.get('task') or {}) if isinstance(data.get('task'), dict) else {}

    def _summary_payload_requires_immediate(self, task_id: str, task_payload: dict[str, Any]) -> bool:
        previous = dict(self._last_summary_payloads.get(task_id) or {})
        if not previous:
            return True
        for key in ('status', 'is_paused', 'is_unread'):
            if previous.get(key) != task_payload.get(key):
                return True
        return False

    def _ensure_task_summary_flush_task(self, task_id: str) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_summary_flush_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_task_summary_to_outbox(key)
            return
        task = loop.create_task(self._run_task_summary_flush(key), name=f'main-runtime-task-summary-flush:{key}')
        self._task_summary_flush_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_summary_flush_task(stored_key, done_task))

    def _clear_task_summary_flush_task(self, task_id: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_summary_flush_tasks.get(task_id)
        if current is done_task:
            self._task_summary_flush_tasks.pop(task_id, None)

    def _enqueue_task_summary_callback(self, payload: dict[str, Any] | None, *, immediate: bool = False) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(payload)
        if not normalized or str(normalized.get('event_type') or '').strip() != 'task.summary.patch':
            return
        task_id = self.normalize_task_id(str(normalized.get('task_id') or '').strip())
        if not task_id:
            return
        task_payload = self._summary_payload_task_payload(normalized)
        if not task_payload:
            return
        self._increment_summary_stat('task_summary_dirty_count')
        if immediate or self._summary_payload_requires_immediate(task_id, task_payload):
            self._flush_task_summary_to_outbox(task_id, normalized)
            return
        now_mono = time.monotonic()
        current = self._pending_task_summaries.get(task_id)
        if current is None:
            self._pending_task_summaries[task_id] = {
                'payload': normalized,
                'first_dirty_mono': now_mono,
                'last_dirty_mono': now_mono,
            }
        else:
            current['payload'] = normalized
            current['last_dirty_mono'] = now_mono
        self._ensure_task_summary_flush_task(task_id)

    def _schedule_pending_task_worker_status_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_worker_status_outbox(limit=500):
            worker_id = str(entry.get('worker_id') or '').strip()
            if worker_id:
                self._schedule_task_worker_status_delivery(worker_id)

    def _schedule_pending_task_summary_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_summary_outbox(limit=500):
            task_id = str(entry.get('task_id') or '').strip()
            if task_id:
                self._schedule_task_summary_delivery(task_id)

    async def _run_task_summary_flush(self, task_id: str) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if not key:
            return
        while True:
            current = dict(self._pending_task_summaries.get(key) or {})
            if not current:
                return
            first_dirty_mono = float(current.get('first_dirty_mono') or time.monotonic())
            last_dirty_mono = float(current.get('last_dirty_mono') or first_dirty_mono)
            due_mono = min(
                last_dirty_mono + _TASK_SUMMARY_DEBOUNCE_SECONDS,
                first_dirty_mono + _TASK_SUMMARY_MAX_WAIT_SECONDS,
            )
            delay_seconds = max(0.0, due_mono - time.monotonic())
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
                continue
            self._flush_task_summary_to_outbox(key)

    def _flush_task_summary_to_outbox(self, task_id: str, payload: dict[str, Any] | None = None) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if self.execution_mode != 'worker' or not key:
            return
        # 删除台账守卫：已删任务的迟到 debounce flush 直接短路，
        # 避免 _last_summary_payloads 等内存态被重新污染。
        try:
            if self.store.is_task_deleted(key):
                self._pending_task_summaries.pop(key, None)
                return
        except Exception:
            pass
        pending_entry = self._pending_task_summaries.pop(key, None)
        normalized = normalize_task_event_payload(payload) if payload is not None else dict(
            (pending_entry or {}).get('payload') or {}
        )
        if not normalized or str(normalized.get('event_type') or '').strip() != 'task.summary.patch':
            return
        session_id = str(normalized.get('session_id') or 'web:shared').strip() or 'web:shared'
        task_payload = self._summary_payload_task_payload(normalized)
        created_at = str(task_payload.get('updated_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_summary_outbox(
                task_id=key,
                session_id=session_id,
                created_at=created_at,
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist task summary outbox for {}', key)
            return
        self._last_summary_payloads[key] = dict(task_payload)
        self._increment_summary_stat('task_summary_flush_count')
        self._increment_summary_stat('task_summary_outbox_write_count')
        self._schedule_task_summary_delivery(key)

    def _schedule_task_summary_delivery(self, _task_id: str | None = None) -> None:
        if self.execution_mode != 'worker':
            return
        current = self._task_summary_delivery_task
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task_summary_delivery_task = loop.create_task(
            self._deliver_task_summary_batches(),
            name=f'main-runtime-task-summary-batch:{self.worker_id or "worker"}',
        )

    async def _deliver_task_summary_batches(self) -> None:
        retry_delays = list(_WORKER_STATUS_CALLBACK_RETRY_DELAYS or [0.0, 0.5, 2.0, 5.0])
        if not retry_delays:
            retry_delays = [0.0]
        attempt_index = 0
        while True:
            delay_seconds = _TASK_SUMMARY_BATCH_WINDOW_SECONDS if attempt_index == 0 else float(
                retry_delays[min(attempt_index, len(retry_delays) - 1)] or 0.0
            )
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entries = list(self.store.list_pending_task_summary_outbox(limit=max(_TASK_SUMMARY_BATCH_MAX_ITEMS * 2, 256)) or [])
            if not entries:
                return
            batch_entries: list[dict[str, Any]] = []
            batch_items: list[dict[str, Any]] = []
            bytes_total = 0
            for entry in entries:
                payload = dict(entry.get('payload') or {})
                encoded_size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                if batch_entries and (
                    len(batch_entries) >= _TASK_SUMMARY_BATCH_MAX_ITEMS
                    or bytes_total + encoded_size > _TASK_SUMMARY_BATCH_MAX_BYTES
                ):
                    break
                batch_entries.append(entry)
                batch_items.append(payload)
                bytes_total += encoded_size
            if not batch_entries:
                return
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='event_batch')
            if not callback_targets:
                for entry in batch_entries:
                    self.store.mark_task_summary_outbox_attempt(
                        str(entry.get('task_id') or ''),
                        attempted_at=now_iso(),
                        error_text='task_event_batch_callback_url_unavailable',
                        expected_version=int(entry.get('version') or 0),
                    )
                attempt_index += 1
                continue
            error_text = 'task_event_batch_callback_failed'
            delivered = False
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload={'items': batch_items},
                        headers=headers,
                        timeout=_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        for entry in batch_entries:
                            self.store.mark_task_summary_outbox_delivered(
                                str(entry.get('task_id') or ''),
                                delivered_at=now_iso(),
                                expected_version=int(entry.get('version') or 0),
                            )
                        self._increment_summary_stat('task_summary_batch_request_count')
                        self._increment_summary_stat('task_summary_batch_item_count', float(len(batch_entries)))
                        attempt_index = 0
                        delivered = True
                        break
                    error_text = f'task_event_batch_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_event_batch_callback_failed').strip() or 'task_event_batch_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            if delivered:
                continue
            for entry in batch_entries:
                self.store.mark_task_summary_outbox_attempt(
                    str(entry.get('task_id') or ''),
                    attempted_at=now_iso(),
                    error_text=error_text,
                    expected_version=int(entry.get('version') or 0),
                )
            attempt_index += 1

    async def _deliver_task_summary_outbox(self, task_id: str) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if not key:
            return
        if self.store.get_task_summary_outbox(key) is None:
            return
        await self._deliver_task_summary_batches()

    def _schedule_task_worker_status_delivery(self, worker_id: str) -> None:
        key = str(worker_id or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_worker_status_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_worker_status_outbox(key), name=f'main-runtime-worker-status:{key}')
        self._task_worker_status_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_worker_status_delivery_task(stored_key, done_task))

    def _clear_task_worker_status_delivery_task(self, worker_id: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_worker_status_delivery_tasks.get(worker_id)
        if current is done_task:
            self._task_worker_status_delivery_tasks.pop(worker_id, None)

    async def _deliver_task_worker_status_outbox(self, worker_id: str) -> None:
        for delay_seconds in _WORKER_STATUS_CALLBACK_RETRY_DELAYS:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_worker_status_outbox(worker_id)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='event')
            if not callback_targets:
                self.store.mark_task_worker_status_outbox_attempt(
                    worker_id,
                    attempted_at=now_iso(),
                    error_text='task_event_callback_url_unavailable',
                )
                return
            error_text = 'task_event_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_worker_status_outbox_delivered(worker_id, delivered_at=now_iso())
                        return
                    error_text = f'task_event_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_event_callback_failed').strip() or 'task_event_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_worker_status_outbox_attempt(
                worker_id,
                attempted_at=now_iso(),
                error_text=error_text,
            )

    def _enqueue_task_stall_callback(self, payload: dict[str, Any]) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_stall_payload(payload)
        if not normalized:
            return
        dedupe_key = str(normalized.get('dedupe_key') or '').strip()
        if not dedupe_key:
            return
        created_at = str(normalized.get('last_visible_output_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_stall_outbox(
                dedupe_key=dedupe_key,
                task_id=str(normalized.get('task_id') or '').strip(),
                session_id=str(normalized.get('session_id') or '').strip() or 'web:shared',
                created_at=created_at,
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist task stall outbox for {}', dedupe_key)
            return
        self._schedule_task_stall_delivery(dedupe_key)

    def _schedule_pending_task_stall_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_stall_outbox(limit=500):
            dedupe_key = str(entry.get('dedupe_key') or '').strip()
            if dedupe_key:
                self._schedule_task_stall_delivery(dedupe_key)

    def _schedule_task_stall_delivery(self, dedupe_key: str) -> None:
        key = str(dedupe_key or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_stall_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_stall_outbox(key), name=f'main-runtime-task-stall:{key}')
        self._task_stall_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_stall_delivery_task(stored_key, done_task))

    def _clear_task_stall_delivery_task(self, dedupe_key: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_stall_delivery_tasks.get(dedupe_key)
        if current is done_task:
            self._task_stall_delivery_tasks.pop(dedupe_key, None)

    async def _deliver_task_stall_outbox(self, dedupe_key: str) -> None:
        retry_delays = [0.0, 0.5, 2.0, 5.0]
        for delay_seconds in retry_delays:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_stall_outbox(dedupe_key)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='stall')
            if not callback_targets:
                self.store.mark_task_stall_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_stall_callback_url_unavailable',
                )
                return
            error_text = 'task_stall_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=_INTERNAL_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_stall_outbox_delivered(dedupe_key, delivered_at=now_iso())
                        return
                    error_text = f'task_stall_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_stall_callback_failed').strip() or 'task_stall_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_stall_outbox_attempt(
                dedupe_key,
                attempted_at=now_iso(),
                error_text=error_text,
            )

    def _enqueue_task_distribution_error_callback(self, payload: dict[str, Any]) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_distribution_error_payload(payload)
        if not normalized:
            return
        dedupe_key = str(normalized.get('dedupe_key') or '').strip()
        if not dedupe_key:
            return
        try:
            self.store.put_task_distribution_error_outbox(
                dedupe_key=dedupe_key,
                task_id=str(normalized.get('task_id') or '').strip(),
                session_id=str(normalized.get('session_id') or '').strip() or 'web:shared',
                created_at=now_iso(),
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist task distribution error outbox for {}', dedupe_key)
            return
        self._schedule_task_distribution_error_delivery(dedupe_key)

    def _schedule_pending_task_distribution_error_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_distribution_error_outbox(limit=500):
            dedupe_key = str(entry.get('dedupe_key') or '').strip()
            if dedupe_key:
                self._schedule_task_distribution_error_delivery(dedupe_key)

    def _schedule_task_distribution_error_delivery(self, dedupe_key: str) -> None:
        key = str(dedupe_key or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_distribution_error_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_distribution_error_outbox(key), name=f'main-runtime-task-distribution-error:{key}')
        self._task_distribution_error_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_distribution_error_delivery_task(stored_key, done_task))

    def _clear_task_distribution_error_delivery_task(self, dedupe_key: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_distribution_error_delivery_tasks.get(dedupe_key)
        if current is done_task:
            self._task_distribution_error_delivery_tasks.pop(dedupe_key, None)

    async def _deliver_task_distribution_error_outbox(self, dedupe_key: str) -> None:
        retry_delays = [0.0, 0.5, 2.0, 5.0]
        for delay_seconds in retry_delays:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_distribution_error_outbox(dedupe_key)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='distribution_error')
            if not callback_targets:
                self.store.mark_task_distribution_error_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_distribution_error_callback_url_unavailable',
                )
                return
            error_text = 'task_distribution_error_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=_INTERNAL_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_distribution_error_outbox_delivered(dedupe_key, delivered_at=now_iso())
                        return
                    error_text = f'task_distribution_error_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_distribution_error_callback_failed').strip() or 'task_distribution_error_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_distribution_error_outbox_attempt(
                dedupe_key,
                attempted_at=now_iso(),
                error_text=error_text,
            )

    def _schedule_pending_task_terminal_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_terminal_outbox(limit=500):
            dedupe_key = str(entry.get('dedupe_key') or '').strip()
            if dedupe_key:
                self._schedule_task_terminal_delivery(dedupe_key)

    def _schedule_task_terminal_delivery(self, dedupe_key: str) -> None:
        key = str(dedupe_key or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_terminal_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        # 常驻补投接管后，web 长时间不可达会让 pending 行全部长挂；用在途上限把
        # 重启风暴压成多轮慢补，而不是一次涌出上百个挂起 task。
        if len(self._task_terminal_delivery_tasks) >= _TASK_TERMINAL_DELIVERY_MAX_INFLIGHT:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_terminal_outbox(key), name=f'main-runtime-task-terminal:{key}')
        self._task_terminal_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_terminal_delivery_task(stored_key, done_task))

    def _clear_task_terminal_delivery_task(self, dedupe_key: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_terminal_delivery_tasks.get(dedupe_key)
        if current is done_task:
            self._task_terminal_delivery_tasks.pop(dedupe_key, None)

    async def _deliver_task_terminal_outbox(self, dedupe_key: str) -> None:
        retry_delays = [0.0, 0.5, 2.0, 5.0]
        for delay_seconds in retry_delays:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_terminal_outbox(dedupe_key)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() in {'delivered', 'abandoned'}:
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='terminal')
            if not callback_targets:
                self.store.mark_task_terminal_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_terminal_callback_url_unavailable',
                    attempt_limit=_TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT,
                )
                return
            error_text = 'task_terminal_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=_INTERNAL_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_terminal_outbox_delivered(dedupe_key, delivered_at=now_iso())
                        return
                    error_text = f'task_terminal_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_terminal_callback_failed').strip() or 'task_terminal_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_terminal_outbox_attempt(
                dedupe_key,
                attempted_at=now_iso(),
                error_text=error_text,
                attempt_limit=_TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT,
            )

    @staticmethod
    def _node_parallelism_settings(config: Any | None) -> tuple[bool, int | None, int | None]:
        agents = getattr(config, 'agents', None) if config is not None else None
        parallelism = getattr(agents, 'node_parallelism', None) if agents is not None else None
        enabled = bool(getattr(parallelism, 'enabled', True)) if parallelism is not None else True
        max_parallel_tool_calls = MainRuntimeService._normalize_optional_parallel_limit(
            getattr(parallelism, 'max_parallel_tool_calls_per_node', None) if parallelism is not None else None
        )
        max_parallel_child_pipelines = MainRuntimeService._normalize_optional_parallel_limit(
            getattr(parallelism, 'max_parallel_child_pipelines_per_node', None) if parallelism is not None else None
        )
        if not enabled:
            return False, 1, 1
        return True, max_parallel_tool_calls, max_parallel_child_pipelines

    @staticmethod
    def _tool_default_timeout_seconds(config: Any | None) -> float | None:
        """统一工具 timeout 全局默认值（秒）；未配置返回 None（执行层用内置默认）。"""
        agents = getattr(config, 'agents', None) if config is not None else None
        try:
            value = float(getattr(agents, 'tool_default_timeout_seconds', 0) or 0)
        except (TypeError, ValueError):
            return None
        return max(1.0, value) if value >= 1 else None

    @staticmethod
    def _node_dispatch_concurrency_settings(config: Any | None) -> dict[str, int]:
        if config is not None and hasattr(config, 'get_node_dispatch_concurrency'):
            try:
                execution = int(config.get_node_dispatch_concurrency('execution'))
                inspection = int(config.get_node_dispatch_concurrency('inspection'))
                return {
                    'execution': max(1, execution),
                    'inspection': max(1, inspection),
                }
            except Exception:
                pass
        main_runtime = getattr(config, 'main_runtime', None) if config is not None else None
        dispatch_config = getattr(main_runtime, 'node_dispatch_concurrency', None) if main_runtime is not None else None
        execution = getattr(dispatch_config, 'execution', 8) if dispatch_config is not None else 8
        inspection = getattr(dispatch_config, 'inspection', 4) if dispatch_config is not None else 4
        return {
            'execution': max(1, int(execution or 8)),
            'inspection': max(1, int(inspection or 4)),
        }

    @staticmethod
    def _event_history_settings(config: Any | None) -> dict[str, Any]:
        main_runtime = getattr(config, 'main_runtime', None) if config is not None else None
        history = getattr(main_runtime, 'event_history', None) if main_runtime is not None else None
        return {
            'enabled': bool(getattr(history, 'enabled', True)) if history is not None else True,
            'dir': str(getattr(history, 'dir', '') or ''),
            'live_patch_persist_window_ms': max(
                0,
                int(getattr(history, 'live_patch_persist_window_ms', 1000) or 1000),
            ),
            'archive_encoding': str(getattr(history, 'archive_encoding', 'gzip') or 'gzip').strip().lower() or 'gzip',
        }

    @staticmethod
    def _disk_guard_policies(config: Any | None) -> DiskPolicies:
        """磁盘治理（P0）：从 config.main_runtime.disk_guard 构造策略；缺节时用默认值。"""
        main_runtime = getattr(config, 'main_runtime', None) if config is not None else None
        guard = getattr(main_runtime, 'disk_guard', None) if main_runtime is not None else None
        defaults = DiskPolicies()
        if guard is None:
            return defaults
        return DiskPolicies(
            write_guard_enabled=bool(getattr(guard, 'write_guard_enabled', defaults.write_guard_enabled)),
            emergency_min_bytes=max(0, int(getattr(guard, 'emergency_min_bytes', defaults.emergency_min_bytes) or 0)),
            emergency_min_ratio=min(max(0.0, float(getattr(guard, 'emergency_min_ratio', defaults.emergency_min_ratio) or 0.0)), 0.5),
            usage_ttl_seconds=max(0.0, float(getattr(guard, 'usage_ttl_seconds', defaults.usage_ttl_seconds) or 0.0)),
            artifact_gzip_threshold_bytes=int(getattr(guard, 'artifact_gzip_threshold_bytes', defaults.artifact_gzip_threshold_bytes) or 0),
            terminal_cleanup_enabled=bool(getattr(guard, 'terminal_cleanup_enabled', defaults.terminal_cleanup_enabled)),
            terminal_temp_dir_cleanup_enabled=bool(getattr(guard, 'terminal_temp_dir_cleanup_enabled', defaults.terminal_temp_dir_cleanup_enabled)),
            auto_pause_enabled=bool(getattr(guard, 'auto_pause_enabled', defaults.auto_pause_enabled)),
            emergency_streak_samples=max(1, int(getattr(guard, 'emergency_streak_samples', defaults.emergency_streak_samples) or 1)),
            emergency_recovery_samples=max(1, int(getattr(guard, 'emergency_recovery_samples', defaults.emergency_recovery_samples) or 1)),
            alert_on_disk_emergency=bool(getattr(guard, 'alert_on_disk_emergency', defaults.alert_on_disk_emergency)),
            detail_retention_days=max(0, int(getattr(guard, 'detail_retention_days', defaults.detail_retention_days) or 0)),
        )

    @staticmethod
    def _duplicate_precheck_settings(config: Any | None) -> dict[str, Any]:
        main_runtime = getattr(config, 'main_runtime', None) if config is not None else None
        precheck = getattr(main_runtime, 'duplicate_precheck', None) if main_runtime is not None else None
        return {
            'llm_review_enabled': bool(getattr(precheck, 'llm_review_enabled', True)) if precheck is not None else True,
        }

    @staticmethod
    def _adaptive_tool_budget_settings(config: Any | None) -> dict[str, Any]:
        agents = getattr(config, 'agents', None) if config is not None else None
        parallelism = getattr(agents, 'node_parallelism', None) if agents is not None else None
        return {
            'enabled': bool(getattr(parallelism, 'adaptive_total_tool_budget_enabled', True)) if parallelism is not None else True,
            'normal_limit': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_normal_limit', 6) or 6)),
            'throttled_limit': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_throttled_limit', 2) or 2)),
            'critical_limit': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_critical_limit', getattr(parallelism, 'adaptive_total_tool_budget_safe_limit', 1)) or 1)),
            'step_up': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_step_up', 1) or 1)),
            'sample_seconds': max(0.1, float(getattr(parallelism, 'adaptive_total_tool_budget_sample_seconds', 1.0) or 1.0)),
            'recover_window_seconds': max(0.1, float(getattr(parallelism, 'adaptive_total_tool_budget_recover_window_seconds', 1.0) or 1.0)),
            'warn_consecutive_samples': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_warn_consecutive_samples', 3) or 3)),
            'safe_consecutive_samples': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_safe_consecutive_samples', 3) or 3)),
            'pressure_snapshot_stale_after_seconds': max(0.1, float(getattr(parallelism, 'adaptive_pressure_snapshot_stale_after_seconds', 3.0) or 3.0)),
            'event_loop_warn_ms': max(0.0, float(getattr(parallelism, 'adaptive_event_loop_warn_ms', 250.0) or 250.0)),
            'event_loop_safe_ms': max(0.0, float(getattr(parallelism, 'adaptive_event_loop_safe_ms', 100.0) or 100.0)),
            'event_loop_critical_ms': max(0.0, float(getattr(parallelism, 'adaptive_event_loop_critical_ms', 1500.0) or 1500.0)),
            'writer_queue_warn': max(1, int(getattr(parallelism, 'adaptive_writer_queue_warn', 50) or 50)),
            'writer_queue_safe': max(1, int(getattr(parallelism, 'adaptive_writer_queue_safe', 10) or 10)),
            'writer_queue_critical': max(1, int(getattr(parallelism, 'adaptive_writer_queue_critical', 100) or 100)),
            'sqlite_write_wait_warn_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_write_wait_warn_ms', 200.0) or 200.0)),
            'sqlite_write_wait_safe_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_write_wait_safe_ms', 50.0) or 50.0)),
            'sqlite_write_wait_critical_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_write_wait_critical_ms', 250.0) or 250.0)),
            'sqlite_query_warn_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_query_warn_ms', 150.0) or 150.0)),
            'sqlite_query_safe_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_query_safe_ms', 30.0) or 30.0)),
            'sqlite_query_critical_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_query_critical_ms', 250.0) or 250.0)),
            'machine_cpu_warn_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_cpu_warn_percent', 85.0) or 85.0)),
            'machine_cpu_safe_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_cpu_safe_percent', 55.0) or 55.0)),
            'machine_cpu_critical_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_cpu_critical_percent', 95.0) or 95.0)),
            'machine_memory_warn_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_memory_warn_percent', 88.0) or 88.0)),
            'machine_memory_safe_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_memory_safe_percent', 75.0) or 75.0)),
            'machine_memory_critical_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_memory_critical_percent', 94.0) or 94.0)),
            'machine_disk_busy_warn_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_disk_busy_warn_percent', 70.0) or 70.0)),
            'machine_disk_busy_safe_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_disk_busy_safe_percent', 35.0) or 35.0)),
            'machine_disk_busy_critical_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_disk_busy_critical_percent', 90.0) or 90.0)),
            'process_cpu_warn_ratio': max(0.0, float(getattr(parallelism, 'adaptive_process_cpu_warn_ratio', 0.85) or 0.85)),
            'process_cpu_safe_ratio': max(0.0, float(getattr(parallelism, 'adaptive_process_cpu_safe_ratio', 0.50) or 0.50)),
            'max_pressure_dwell_seconds': max(0.0, float(getattr(parallelism, 'adaptive_pressure_max_dwell_seconds', 60.0) or 0.0)),
            'max_tool_wait_ms': max(0.0, float(getattr(parallelism, 'adaptive_pressure_max_tool_wait_ms', 30000.0) or 0.0)),
            'local_recovery_enabled': bool(getattr(parallelism, 'adaptive_pressure_local_recovery_enabled', True)) if parallelism is not None else True,
            'gate_stale_after_seconds': max(0.0, float(getattr(parallelism, 'adaptive_pressure_gate_stale_after_seconds', 10.0) or 0.0)),
            'gate_close_on_machine_critical': bool(getattr(parallelism, 'adaptive_pressure_gate_close_on_machine_critical', False)) if parallelism is not None else False,
        }

    @staticmethod
    def _resource_reload_poll_interval_ms(config: Any | None) -> int:
        resources = getattr(config, 'resources', None) if config is not None else None
        reload_cfg = getattr(resources, 'reload', None) if resources is not None else None
        raw = getattr(reload_cfg, 'poll_interval_ms', None)
        if raw is None:
            return 1000
        return max(0, int(raw))

    @staticmethod
    def _resource_state_names_changed(
        before_state: dict[str, dict[str, str]] | None,
        after_state: dict[str, dict[str, str]] | None,
        section: str,
    ) -> list[str]:
        previous = dict((before_state or {}).get(section) or {})
        current = dict((after_state or {}).get(section) or {})
        return sorted(
            name
            for name in set(previous) | set(current)
            if previous.get(name) != current.get(name)
        )

    def _resource_tree_state_snapshot(self) -> dict[str, dict[str, str]]:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'capture_resource_tree_state'):
            return {}
        try:
            snapshot = manager.capture_resource_tree_state()
        except Exception:
            return {}
        return dict(snapshot or {})

    def _record_resource_tree_state(
        self,
        state: dict[str, dict[str, str]] | None = None,
        *,
        checked_at: float | None = None,
    ) -> None:
        self._resource_tree_state_cache = dict(state or self._resource_tree_state_snapshot() or {})
        self._resource_tree_state_checked_at = float(time.perf_counter() if checked_at is None else checked_at)

    async def _sync_catalog_targets(
        self,
        *,
        skill_ids: set[str] | None = None,
        tool_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        normalized_skill_ids = {
            str(item or '').strip()
            for item in (skill_ids or set())
            if str(item or '').strip()
        }
        normalized_tool_ids = {
            str(item or '').strip()
            for item in (tool_ids or set())
            if str(item or '').strip()
        }
        return {
            'catalog_synced': False,
            'skill_ids': sorted(normalized_skill_ids),
            'tool_ids': sorted(normalized_tool_ids),
        }

    async def maybe_refresh_external_resource_changes(
        self,
        *,
        session_id: str = 'web:shared',
        force: bool = False,
    ) -> dict[str, Any]:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'capture_resource_tree_state'):
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        now = time.perf_counter()
        interval_ms = max(0, int(getattr(self, '_resource_tree_state_poll_interval_ms', 0) or 0))
        before_state = dict(getattr(self, '_resource_tree_state_cache', None) or {})
        last_checked_at = float(getattr(self, '_resource_tree_state_checked_at', 0.0) or 0.0)
        if not before_state:
            self._record_resource_tree_state(checked_at=now)
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        if (
            not force
            and interval_ms > 0
            and last_checked_at > 0.0
            and ((now - last_checked_at) * 1000.0) < interval_ms
        ):
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        after_state = self._resource_tree_state_snapshot()
        if after_state == before_state:
            self._record_resource_tree_state(after_state, checked_at=now)
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        skill_ids = self._resource_state_names_changed(before_state, after_state, 'skills')
        tool_ids = self._resource_state_names_changed(before_state, after_state, 'tools')
        refresh_result = self.refresh_changed_resources(
            before_state,
            trigger='external-resource-generation-check',
            session_id=session_id,
        )
        self._node_context_selection_cache = {}
        self._record_resource_tree_state(after_state, checked_at=now)
        sync_result = await self._sync_catalog_targets(
            skill_ids=set(skill_ids),
            tool_ids=set(tool_ids),
        )
        return {
            'refreshed': True,
            'resources': refresh_result,
            **sync_result,
        }

    @staticmethod
    def _repair_legacy_display_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = str(value or '')
        if not text:
            return text
        text = re.sub(r'^鏈€缁堥獙鏀(?:讹細|:|：|\?)?', '最终验收:', text)
        replacements = (
            ('鏍稿\ue1ee鏈€缁堢粨鏋滄槸鍚︽弧瓒宠\ue6e6姹傘€?', '核对最终结果是否满足要求。'),
            ('妫€鏌ユ渶缁堢粨鏋滄槸鍚︽弧瓒宠\ue6e6姹傘€?', '检查最终结果是否满足要求。'),
            ('妫€鏌?child 杈撳嚭銆?', '检查 child 输出。'),
            ('妫€鏌ュ叕鍛婅崏绋挎槸鍚︽弧瓒充氦浠樿\ue6e6姹傘€?', '检查公告草稿是否满足交付要求。'),
            ('楠屾敹閫氳繃', '验收通过'),
            ('鑷\ue043富鎵ц\ue511', '自主执行'),
            ('杩涜\ue511涓?', '进行中'),
            ('鏈€鏂伴樁娈电洰鏍?', '最新阶段目标'),
        )
        for source, target in replacements:
            text = text.replace(source, target)
        return text

    @classmethod
    def _repair_legacy_display_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._repair_legacy_display_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._repair_legacy_display_payload(item) for item in value]
        return cls._repair_legacy_display_text(value)

    @staticmethod
    def _node_latest_context_display_payload(value: Any) -> Any | None:
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return None
        request_messages = value.get('request_messages')
        provider_request_body = value.get('provider_request_body')
        provider_input = provider_request_body.get('input') if isinstance(provider_request_body, dict) else None
        model_messages = value.get('model_messages')
        for candidate in (request_messages, provider_input, model_messages):
            if isinstance(candidate, list) and candidate:
                return candidate
            if isinstance(candidate, (dict, str)) and candidate:
                return candidate
        if isinstance(request_messages, list):
            return request_messages
        if isinstance(provider_input, list):
            return provider_input
        if isinstance(model_messages, list):
            return model_messages
        return None

    @classmethod
    def _render_node_latest_context_content(cls, raw_content: Any) -> str:
        text = str(raw_content or '')
        if not text.strip():
            return ''
        try:
            parsed = json.loads(text)
        except Exception:
            return str(cls._repair_legacy_display_text(text))
        display_payload = cls._node_latest_context_display_payload(parsed)
        rendered_payload = cls._repair_legacy_display_payload(display_payload if display_payload is not None else parsed)
        if isinstance(rendered_payload, str):
            return str(rendered_payload)
        return json.dumps(rendered_payload, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _normalize_optional_parallel_limit(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    @staticmethod
    def _normalize_optional_limit(value: Any, *, default: int | None) -> int | None:
        if value is _UNSET:
            value = default
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    def ensure_runtime_config_current(self, force: bool = False, reason: str = 'runtime') -> bool:
        if force:
            security = get_bootstrap_security_service(
                getattr(getattr(self, '_app_config', None), 'workspace_path', None)
            )
            security.reload_overlay_from_disk()
        config, revision, changed = get_runtime_config(force=force)
        if not changed and int(getattr(self, '_runtime_model_revision', 0) or 0) == int(revision or 0):
            return False
        self._app_config = config
        if hasattr(self._chat_backend, '_config'):
            self._chat_backend._config = config
        self._default_max_depth = max(0, int(getattr(config.main_runtime, 'default_max_depth', 1) or 0))
        self._hard_max_depth = max(self._default_max_depth, int(getattr(config.main_runtime, 'hard_max_depth', self._default_max_depth) or self._default_max_depth))
        self._max_task_nodes = max(1, int(getattr(config.main_runtime, 'max_task_nodes', 200) or 200))
        self._node_tool_timeout_ceiling_seconds = max(
            1.0,
            float(
                getattr(config.main_runtime, 'node_tool_timeout_ceiling_seconds', 900.0) or 900.0
            ),
        )
        if hasattr(self, 'node_runner') and self.node_runner is not None:
            self.node_runner._max_task_nodes = self._max_task_nodes
            self.node_runner._node_tool_timeout_ceiling_seconds = self._node_tool_timeout_ceiling_seconds
        self.node_runner._execution_model_refs = list(config.get_role_model_keys('execution'))
        self.node_runner._acceptance_model_refs = list(config.get_role_model_keys('inspection') or config.get_role_model_keys('execution'))
        # route plan 是权威来源；refs 只是候选展开视图。检验缺省时继承整份执行 route plan。
        execution_routes = build_model_route_plan(config, 'execution', revision=revision)
        inspection_routes = build_model_route_plan(config, 'inspection', revision=revision)
        if not inspection_routes.routes:
            inspection_routes = execution_routes
        self._apply_model_route_plans(execution_routes, inspection_routes, revision=revision)
        self.node_runner._execution_max_iterations = config.get_role_max_iterations('execution')
        self.node_runner._acceptance_max_iterations = config.get_role_max_iterations('inspection')
        self.node_runner._execution_max_concurrency = config.get_role_max_concurrency('execution')
        self.node_runner._acceptance_max_concurrency = config.get_role_max_concurrency('inspection')
        self.node_runner._tool_default_timeout_seconds = self._tool_default_timeout_seconds(config)
        dispatch_limits = self._node_dispatch_concurrency_settings(config)
        self.task_actor_service.configure_node_dispatch_limits(
            execution=dispatch_limits['execution'],
            inspection=dispatch_limits['inspection'],
        )
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(config)
        self._react_loop._parallel_tool_calls_enabled = parallel_enabled
        self._react_loop._max_parallel_tool_calls = max_parallel_tool_calls
        self.node_runner._max_parallel_child_pipelines = max_parallel_child_pipelines
        self.node_runner._parallel_child_pipelines_enabled = parallel_enabled
        adaptive_budget_settings = self._adaptive_tool_budget_settings(config)
        reload_sample_seconds = float(adaptive_budget_settings['sample_seconds'])
        reload_gate_stale = float(adaptive_budget_settings['gate_stale_after_seconds'])
        if reload_gate_stale > 0.0:
            reload_gate_stale = max(reload_gate_stale, reload_sample_seconds * 3.0)
        self._pressure_gate_stale_after_seconds = reload_gate_stale
        self._pressure_gate_close_on_machine_critical = bool(adaptive_budget_settings['gate_close_on_machine_critical'])
        if self.adaptive_tool_budget_controller is not None:
            self.adaptive_tool_budget_controller.configure(
                normal_limit=int(adaptive_budget_settings['normal_limit']),
                throttled_limit=int(adaptive_budget_settings['throttled_limit']),
                critical_limit=int(adaptive_budget_settings['critical_limit']),
                step_up=int(adaptive_budget_settings['step_up']),
            )
        if self.model_key_concurrency_controller is not None:
            self.model_key_concurrency_controller.configure(
                resolve_model_limits=self._resolve_model_limit_payload,
                on_availability_changed=self.node_turn_controller.poke if self.node_turn_controller is not None else None,
            )
        if self.node_turn_controller is not None:
            self.node_turn_controller.configure(
                gate_supplier=self._node_turn_gate_allowed,
            )
        if self.tool_pressure_monitor is not None:
            self.tool_pressure_monitor.configure(
                sample_seconds=float(adaptive_budget_settings['sample_seconds']),
                recover_window_seconds=float(adaptive_budget_settings['recover_window_seconds']),
                warn_consecutive_samples=int(adaptive_budget_settings['warn_consecutive_samples']),
                safe_consecutive_samples=int(adaptive_budget_settings['safe_consecutive_samples']),
                pressure_snapshot_stale_after_seconds=float(adaptive_budget_settings['pressure_snapshot_stale_after_seconds']),
                event_loop_warn_ms=float(adaptive_budget_settings['event_loop_warn_ms']),
                event_loop_safe_ms=float(adaptive_budget_settings['event_loop_safe_ms']),
                event_loop_critical_ms=float(adaptive_budget_settings['event_loop_critical_ms']),
                writer_queue_warn=int(adaptive_budget_settings['writer_queue_warn']),
                writer_queue_safe=int(adaptive_budget_settings['writer_queue_safe']),
                writer_queue_critical=int(adaptive_budget_settings['writer_queue_critical']),
                sqlite_write_wait_warn_ms=float(adaptive_budget_settings['sqlite_write_wait_warn_ms']),
                sqlite_write_wait_safe_ms=float(adaptive_budget_settings['sqlite_write_wait_safe_ms']),
                sqlite_write_wait_critical_ms=float(adaptive_budget_settings['sqlite_write_wait_critical_ms']),
                sqlite_query_warn_ms=float(adaptive_budget_settings['sqlite_query_warn_ms']),
                sqlite_query_safe_ms=float(adaptive_budget_settings['sqlite_query_safe_ms']),
                sqlite_query_critical_ms=float(adaptive_budget_settings['sqlite_query_critical_ms']),
                machine_cpu_warn_percent=float(adaptive_budget_settings['machine_cpu_warn_percent']),
                machine_cpu_safe_percent=float(adaptive_budget_settings['machine_cpu_safe_percent']),
                machine_cpu_critical_percent=float(adaptive_budget_settings['machine_cpu_critical_percent']),
                machine_memory_warn_percent=float(adaptive_budget_settings['machine_memory_warn_percent']),
                machine_memory_safe_percent=float(adaptive_budget_settings['machine_memory_safe_percent']),
                machine_memory_critical_percent=float(adaptive_budget_settings['machine_memory_critical_percent']),
                machine_disk_busy_warn_percent=float(adaptive_budget_settings['machine_disk_busy_warn_percent']),
                machine_disk_busy_safe_percent=float(adaptive_budget_settings['machine_disk_busy_safe_percent']),
                machine_disk_busy_critical_percent=float(adaptive_budget_settings['machine_disk_busy_critical_percent']),
                process_cpu_warn_ratio=float(adaptive_budget_settings['process_cpu_warn_ratio']),
                process_cpu_safe_ratio=float(adaptive_budget_settings['process_cpu_safe_ratio']),
                max_pressure_dwell_seconds=float(adaptive_budget_settings['max_pressure_dwell_seconds']),
                max_tool_wait_ms=float(adaptive_budget_settings['max_tool_wait_ms']),
                local_recovery_enabled=bool(adaptive_budget_settings['local_recovery_enabled']),
            )
        # Resource manager config binding and reload are handled by refresh_loop_runtime_config().
        # Rebinding here clears dynamic tool instances before CEO exposure is assembled.
        self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        self._runtime_model_revision = int(revision or 0)
        return True

    def _normalize_task_metadata(
        self,
        *,
        session_id: str,
        metadata: dict[str, Any] | None,
        task_prompt: str = '',
    ) -> dict[str, Any]:
        payload = dict(metadata or {})
        raw_session_id = self._normalize_session_key(session_id)
        payload.setdefault('origin_session_id', raw_session_id)
        if raw_session_id.startswith('web:'):
            payload['memory_scope'] = normalize_memory_scope(
                payload.get('memory_scope'),
                fallback_channel=DEFAULT_WEB_MEMORY_SCOPE['channel'],
                fallback_chat_id=DEFAULT_WEB_MEMORY_SCOPE['chat_id'],
            )
        else:
            payload['memory_scope'] = normalize_memory_scope(
                payload.get('memory_scope'),
                fallback_session_key=raw_session_id,
            )
        payload.pop('continuation_of_task_id', None)
        payload.pop('created_by_source', None)
        core_requirement = str(payload.get('core_requirement') or '').strip() or str(task_prompt or '').strip()
        if core_requirement:
            payload['core_requirement'] = core_requirement
        else:
            payload.pop('core_requirement', None)
        payload['execution_policy'] = normalize_execution_policy_metadata(payload.get('execution_policy')).model_dump(mode='json')
        payload['final_acceptance'] = normalize_final_acceptance_metadata(payload.get('final_acceptance')).model_dump(mode='json')
        return payload

    def _task_memory_scope(self, task: TaskRecord | None) -> dict[str, str]:
        if task is None:
            return dict(DEFAULT_WEB_MEMORY_SCOPE)
        return normalize_memory_scope(
            (task.metadata or {}).get('memory_scope') if isinstance(task.metadata, dict) else None,
            fallback_session_key=task.session_id,
        )

    def _subject(self, *, actor_role: str, session_id: str, task_id: str | None = None, node_id: str | None = None) -> PermissionSubject:
        return PermissionSubject(user_key=session_id, session_id=session_id, task_id=task_id, node_id=node_id, actor_role=actor_role)

    def list_effective_tool_names(self, *, actor_role: str, session_id: str) -> list[str]:
        resource_manager = getattr(self, '_resource_manager', None)
        supported = sorted((resource_manager.tool_instances().keys() if resource_manager is not None else []))
        resource_registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if resource_registry is None or policy_engine is None:
            return list(supported)
        return list_effective_tool_names(subject=self._subject(actor_role=actor_role, session_id=session_id), supported_tool_names=supported, resource_registry=resource_registry, policy_engine=policy_engine)

    def list_visible_skill_resources(self, *, actor_role: str, session_id: str):
        records = list(self.resource_registry.list_skill_resources() or [])
        visible_ids = set(list_effective_skill_ids(subject=self._subject(actor_role=actor_role, session_id=session_id), available_skill_ids=[item.skill_id for item in records], policy_engine=self.policy_engine))
        return [item for item in records if item.skill_id in visible_ids]

    def list_contract_visible_skill_resources(self, *, actor_role: str, session_id: str):
        resource_registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if resource_registry is None or policy_engine is None:
            return list(self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id) or [])
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        items = []
        find_role_policy = getattr(policy_engine, '_find_role_policy', None)
        for record in list(resource_registry.list_skill_resources() or []):
            skill_id = str(getattr(record, 'skill_id', '') or '').strip()
            if not skill_id or not bool(getattr(record, 'enabled', True)):
                continue
            allowed_roles = {
                str(role or '').strip()
                for role in list(getattr(record, 'allowed_roles', []) or [])
                if str(role or '').strip()
            }
            if str(actor_role or '').strip() not in allowed_roles:
                continue
            if callable(find_role_policy):
                policy = find_role_policy(
                    subject=subject,
                    resource_kind='skill',
                    resource_id=skill_id,
                    action_id='load',
                )
                if policy is not None and str(getattr(policy, 'effect', '') or '').strip().lower() != 'allow':
                    continue
            items.append(record)
        return items

    def list_visible_tools(self, *, actor_role: str, session_id: str):
        visible_names = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        tools = []
        for record in self.resource_registry.list_tools():
            if not bool(getattr(record, 'agent_visible', True)):
                continue
            decision = self.policy_engine.evaluate_tool(subject=subject, tool_id=record.tool_id)
            tool_visible = record.tool_id in visible_names
            metadata = dict(getattr(record, 'metadata', {}) or {})
            if decision.allowed and tool_visible:
                tools.append(record)
                continue
            if self._should_expose_unavailable_tool(actor_role=actor_role, record=record):
                tools.append(record.model_copy(update={'metadata': {**metadata, 'context_lookup_only': True}}))
                continue
            if self._should_expose_context_lookup_only_tool(
                actor_role=actor_role,
                record=record,
                decision=decision,
                tool_visible=tool_visible,
            ):
                tools.append(record.model_copy(update={'metadata': {**metadata, 'context_lookup_only': True}}))
        return tools

    def execution_visible_tool_lightweight_items(self, *, actor_role: str, session_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for record in list(self.list_visible_tools(actor_role=actor_role, session_id=session_id) or []):
            tool_id = str(getattr(record, "tool_id", "") or "").strip()
            if not tool_id:
                continue
            metadata = dict(getattr(record, "metadata", {}) or {})
            items.append(
                {
                    "tool_id": tool_id,
                    "display_name": str(getattr(record, "display_name", "") or tool_id).strip() or tool_id,
                    "description": str(getattr(record, "description", "") or "").strip(),
                    "l0": str(metadata.get("l0") or getattr(record, "l0", "") or "").strip(),
                    "l1": str(metadata.get("l1") or getattr(record, "l1", "") or "").strip(),
                }
            )
        return items

    @staticmethod
    def _stable_execution_visible_tool_items(
        *,
        items: list[dict[str, Any]],
        visible_tool_names: list[str],
    ) -> list[dict[str, Any]]:
        visible_index = {
            str(name or '').strip(): index
            for index, name in enumerate(list(visible_tool_names or []))
            if str(name or '').strip()
        }
        fallback_index = len(visible_index) + 10_000

        def _item_sort_key(item: dict[str, Any]) -> tuple[int, str]:
            tool_id = str((item or {}).get('tool_id') or '').strip()
            return (visible_index.get(tool_id, fallback_index), tool_id)

        return sorted(list(items or []), key=_item_sort_key)

    @staticmethod
    def _canonicalize_execution_promoted_tool_names(
        *,
        tool_names: list[str],
        visible_tool_items: list[dict[str, Any]],
    ) -> list[str]:
        visible_ids = {
            str((item or {}).get('tool_id') or '').strip()
            for item in list(visible_tool_items or [])
        }
        canonical_names: list[str] = []
        for raw_name in list(tool_names or []):
            name = str(raw_name or '').strip()
            if not name or name not in visible_ids or name in canonical_names:
                continue
            canonical_names.append(name)
        return canonical_names

    @staticmethod
    def _execution_fixed_builtin_tool_names(*, visible_tool_names: list[str]) -> list[str]:
        visible_name_set = {
            str(item or '').strip()
            for item in list(visible_tool_names or [])
            if str(item or '').strip()
        }
        return [
            name
            for name in _NODE_FIXED_BUILTIN_TOOL_NAMES
            if name in visible_name_set
        ]

    def _persist_pinned_tool_schema_bundle(
        self,
        *,
        task_id: str,
        node_id: str,
        schemas_by_name: dict[str, Any],
        build_reason: str = '',
    ) -> dict[str, Any]:
        """把整张渲染好的 `tools[]` 原字节存成内容引用，返回 {ref, revision, names}。

        只在清单发生变化的那一跳调用：那一刻每条参数表都拿得到，存下来是零成本的，
        而它之后就是唯一不会消失的那份——活实例会因删除消失、删除前快照随进程清空、
        上一跳原文会被逐跳剪枝。存的是 list 而不是 dict，读侧与 `actual_tool_schemas`
        共用同一个按名索引函数。

        singleton 是就地覆盖、复用同一个 artifact_id ⇒ **ref 不随内容变**（`revision` 才随
        内容变，取落盘文本的 sha256）。只按 ref 做进程内缓存会在重印之后继续供旧字节。
        任何失败返回空表：调用侧记一次可数控数，不静默退化。
        """
        if not schemas_by_name:
            return {}
        store = getattr(self, 'artifact_store', None)
        create_singleton = getattr(store, 'create_or_replace_singleton_text_artifact', None)
        if not callable(create_singleton):
            return {}
        names = [str(name or '').strip() for name in schemas_by_name if str(name or '').strip()]
        payload = {
            'task_id': str(task_id or ''),
            'node_id': str(node_id or ''),
            'names': names,
            'tool_schemas': [schemas_by_name[name] for name in schemas_by_name],
            'built_at': now_iso(),
            'build_reason': str(build_reason or '').strip(),
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            # singleton：同 (task, node, kind) 只留一份，重写就地覆盖——清单每变一次跟着换，
            # 不逐跳累积，也不会被按 kind 的剪枝扫到。
            artifact = create_singleton(
                task_id=str(task_id or '').strip(),
                node_id=str(node_id or '').strip(),
                kind=_PINNED_SCHEMA_BUNDLE_KIND,
                title='pinned tools[] schema bundle',
                content=text,
                extension='.json',
                mime_type='application/json',
            )
        except Exception:
            return {}
        artifact_id = str(getattr(artifact, 'artifact_id', '') or '').strip()
        # 存储侧的 artifact_id 本身就带 `artifact:` 前缀；手拼会把引用写成
        # `artifact:artifact:<hex>`（旧格式仍可解析，但那是别人踩过的坑）。走同一个规范化函数。
        return {
            'ref': artifact_ref_from_id(artifact_id),
            'revision': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'names': names,
        }

    def _pinned_tool_schema_bundle_index(self, *, bundle_ref: str, bundle_revision: str = '') -> dict[str, Any]:
        """读回钉住清单的整表原字节，按工具名索引；拿不到返回空表。

        进程内缓存的键是 `(ref, revision)`：singleton 就地覆盖复用同一个 ref，只按 ref 缓存会
        在重印之后继续供旧字节。revision 为空（存量帧里没这个字段）时不写缓存、每次读盘，
        宁可能一次多读也不供可能过期的内容。
        """
        ref = str(bundle_ref or '').strip()
        if not ref:
            return {}
        revision = str(bundle_revision or '').strip()
        cache = getattr(self, '_pinned_bundle_index_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self._pinned_bundle_index_cache = cache
        cache_key = (ref, revision)
        if revision and cache_key in cache:
            return dict(cache[cache_key])
        resolver = getattr(getattr(self, 'log_service', None), 'resolve_content_ref', None)
        if not callable(resolver):
            return {}
        try:
            text = str(resolver(ref) or '')
        except Exception:
            return {}
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        index = index_schemas_by_name(list(payload.get('tool_schemas') or []))
        if revision and index:
            cache[cache_key] = dict(index)
            while len(cache) > _PINNED_BUNDLE_INDEX_CACHE_LIMIT:
                cache.pop(next(iter(cache)))
        return index

    def _build_node_pinned_tool_bundle(
        self,
        *,
        task_id: str,
        node_id: str,
        provider_tool_names: list[str],
        visible_tools: dict[str, Any] | None = None,
        prior_bundle_index: dict[str, Any] | None = None,
        fallback_schemas: dict[str, Any] | None = None,
        build_reason: str = '',
        prior_index_first: bool = False,
    ) -> dict[str, Any]:
        """按"这一跳拿得到的字节"重建整表并落盘，返回 {ref, revision, index, placeholders}。

        来源次序按重建的**目的**分两种：
        - 播种/边界重印（`prior_index_first=False`）：派发字典里的实例 → 资源管理器里的活实例
          （RBAC 收回、水合撤窄时实例还在）→ 上一份整表 → 调用方给的声明侧兜底。整表在重印
          边界就是该换新字节的那一刻，运维在 Tool Admin 里改的参数定义在这一跳被吸收。
        - 兼容重建（`prior_index_first=True`，冻结跳发现整表缺名）：先原样复用上一份整表，只为
          缺的那个名字去读实例。否则一次无关的增删会顺带把别人的字节换掉——那就是"在重印边界
          之外断前缀"，正是单源化要消灭的形状。

        写的那刻成员名单与渲染结果同源，所以读侧可以只信它。
        """
        resource_manager = getattr(self, '_resource_manager', None)
        instance_getter = getattr(resource_manager, 'tool_instances', None)
        declared_instances = dict(instance_getter() or {}) if callable(instance_getter) else {}
        bundle_visible_tools = dict(visible_tools or {})
        prior_index = dict(prior_bundle_index or {})
        fallback = dict(fallback_schemas or {})
        rendered: dict[str, Any] = {}
        placeholders: list[str] = []
        for candidate_name in provider_tool_names:
            normalized_name = str(candidate_name or '').strip()
            if not normalized_name or normalized_name in rendered:
                continue
            payload: Any = None
            live_sources = (bundle_visible_tools.get(normalized_name), declared_instances.get(normalized_name))
            carried_sources = (prior_index, fallback)
            sources: tuple[Any, ...]
            carried: Any
            if prior_index_first:
                sources = (*carried_sources, live_sources[0], live_sources[1])
            else:
                sources = (live_sources[0], live_sources[1], *carried_sources)
            for source in sources:
                if isinstance(source, dict):
                    carried = source.get(normalized_name)
                    if isinstance(carried, dict):
                        payload = carried
                        break
                    continue
                to_schema = getattr(source, 'to_model_schema', None)
                if not callable(to_schema):
                    continue
                try:
                    payload = to_schema()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    break
            if not isinstance(payload, dict):
                # 三份来源都取不到才占位。占位跟着进整表（阶段 2 既定语义）：不写进整表的话，
                # 下一跳又会缺名重建一次并再记一次 fault，"冻结跳零 IO"与 fault 信噪比一起塌。
                payload = self._placeholder_pinned_tool_schema(normalized_name)
                placeholders.append(normalized_name)
            rendered[normalized_name] = payload
        if not rendered:
            return {}
        persisted = self._persist_pinned_tool_schema_bundle(
            task_id=task_id,
            node_id=node_id,
            schemas_by_name=rendered,
            build_reason=build_reason,
        )
        if not persisted.get('ref'):
            return {'ref': '', 'revision': '', 'index': dict(rendered), 'placeholders': placeholders}
        return {
            'ref': persisted['ref'],
            'revision': persisted.get('revision') or '',
            'index': dict(rendered),
            'placeholders': placeholders,
        }

    @staticmethod
    def _placeholder_pinned_tool_schema(name: str) -> dict[str, Any]:
        """四档都取不到时的最后一格：与 `retired_schemas.placeholder_tool_schema` 同一份字节。

        钉住的是成员，不是编造契约——空 `properties` 加一句实话，模型调它拿到的仍是执行准入
        的拒绝回执。断点位置与"直接删名"完全相同（都从这一条的起始字节起断），所以这格不省
        缓存，省的是语义：名字不会在重印边界之外悄悄消失。两条车道共用这一个生成器。
        """
        return placeholder_tool_schema(name)

    def _node_last_sent_tool_schema_index(self, *, actual_request_ref: str) -> dict[str, Any]:
        """上一跳实际发出的 `tools[]` 原文，按工具名索引；拿不到就返回空表。

        只在"清单里的名字既无活实例也无删除前快照"时被调用，用作最后一档兜底：原文是
        逐字节的历史事实，复用它不会改变 `tools[]` 的字节，也就不会在重印边界之外断前缀。
        工件被内存/磁盘保护降级成 minimal 时原文本就不在，返回空表——那一跳允许名字退场一次，
        下一个重印边界自然收敛，不为此新增任何拒绝面。
        """
        ref = str(actual_request_ref or '').strip()
        if not ref:
            return {}
        resolver = getattr(getattr(self, 'log_service', None), 'resolve_content_ref', None)
        if not callable(resolver):
            return {}
        try:
            text = str(resolver(ref) or '')
        except Exception:
            return {}
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return index_schemas_by_name(
            list(payload.get('actual_tool_schemas') or payload.get('tool_schemas') or [])
        )

    def _select_model_visible_tool_schema_payload(
        self,
        *,
        task_id: str,
        node_id: str,
        node_kind: str,
        visible_tools: dict[str, Tool],
        runtime_context: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_node_kind = str(node_kind or '').strip().lower()
        if normalized_node_kind not in {'execution', 'acceptance'}:
            return {
                'tool_names': list(dict(visible_tools or {}).keys()),
                'lightweight_tool_ids': [],
                'schema_chars': 0,
                'trace': {'mode': 'all_visible'},
            }
        runtime_payload = dict(runtime_context or {})
        store = getattr(self, 'store', None)
        get_task = getattr(store, 'get_task', None) if store is not None else None
        get_node = getattr(store, 'get_node', None) if store is not None else None
        task = get_task(str(task_id or '').strip()) if callable(get_task) else None
        node = get_node(str(node_id or '').strip()) if callable(get_node) else None
        session_id = str(
            runtime_payload.get('session_key')
            or getattr(task, 'session_id', '')
            or 'web:shared'
        ).strip() or 'web:shared'
        actor_role = str(
            runtime_payload.get('actor_role')
            or ('inspection' if normalized_node_kind == 'acceptance' else 'execution')
        ).strip() or ('inspection' if normalized_node_kind == 'acceptance' else 'execution')
        ordered_visible_tool_names = self._normalized_tool_name_list(list(dict(visible_tools or {}).keys()))
        visible_rbac_tool_names = self._normalized_tool_name_list(list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or []))
        if not visible_rbac_tool_names:
            visible_rbac_tool_names = list(ordered_visible_tool_names)
        # 常驻判定与候选派生的上界是治理可见集，不是本轮已构建的对象字典。对象字典按
        # 候选回读 frame 构建（_tool_provider -> _restore_node_context_selection_entry），
        # 拿它当上界会让一次塌缩自我放大成"能 load 不能 promote、也调不动"的死区。
        membership_visible_tool_names = self._normalized_tool_name_list(
            [*visible_rbac_tool_names, *ordered_visible_tool_names]
        )
        stable_lightweight_items = self._stable_execution_visible_tool_items(
            items=list(self.execution_visible_tool_lightweight_items(actor_role=actor_role, session_id=session_id) or []),
            visible_tool_names=visible_rbac_tool_names,
        )
        lightweight_items = list(stable_lightweight_items)
        # 本轮的在场视图：跳起点算一次（react_loop 把它放进 runtime_context），四条装配路
        # 共用同一份，不再各算各的（D12）。水合台账已删，这里没有任何跨跳记忆。
        presence_by_tool_id = runtime_payload.get('tool_contract_presence')
        if not isinstance(presence_by_tool_id, dict):
            presence_by_tool_id = {}
        repair_required_tool_names = {
            str(name or '').strip()
            for name, tool in dict(visible_tools or {}).items()
            if isinstance(tool, RepairRequiredTool)
        }
        contract_present_tool_names = [
            name
            for name in membership_visible_tool_names
            if name in presence_by_tool_id and name not in repair_required_tool_names
        ]
        always_callable_tool_names: list[str] = []
        lightweight_executor_names = {
            str((item or {}).get('tool_id') or '').strip()
            for item in list(lightweight_items or [])
            if str((item or {}).get('tool_id') or '').strip()
        }
        control_tool_names = self._execution_control_tool_names(
            node_kind=normalized_node_kind,
            can_spawn_children=bool(getattr(node, 'can_spawn_children', normalized_node_kind == 'execution')),
        )
        for name in [
            *control_tool_names,
            *self._execution_fixed_builtin_tool_names(visible_tool_names=membership_visible_tool_names),
        ]:
            normalized = str(name or '').strip()
            if normalized and normalized in membership_visible_tool_names and normalized not in always_callable_tool_names:
                always_callable_tool_names.append(normalized)
        for name in membership_visible_tool_names:
            if name in lightweight_executor_names or name in always_callable_tool_names:
                continue
            always_callable_tool_names.append(name)
        schema_size_by_executor = {
            name: len(json.dumps(tool.to_model_schema(), ensure_ascii=False, sort_keys=True))
            for name, tool in dict(visible_tools or {}).items()
        }
        selection = build_execution_tool_selection(
            prompt=str(getattr(node, 'prompt', '') or ''),
            goal=str(getattr(node, 'goal', '') or ''),
            core_requirement=str(getattr(task, 'metadata', {}).get('core_requirement') or getattr(node, 'prompt', '') or getattr(node, 'goal', '') or ''),
            visible_tool_items=list(lightweight_items),
            visible_tool_names=list(membership_visible_tool_names),
            always_callable_tool_names=list(always_callable_tool_names),
            contract_present_tool_names=list(contract_present_tool_names),
            schema_size_by_executor=schema_size_by_executor,
        )
        full_callable_tool_names = self._normalized_tool_name_list(list(selection.callable_tool_names or []))
        model_visible_callable_tool_names = self._model_visible_callable_tool_names_for_node(
            task_id=str(task_id or '').strip(),
            node_id=str(node_id or '').strip(),
            node_kind=normalized_node_kind,
            callable_tool_names=full_callable_tool_names,
        )
        log_service = getattr(self, 'log_service', None)
        read_runtime_frame = getattr(log_service, 'read_runtime_frame', None)
        prior_selected_tool_names: list[str] = []
        prior_provider_tool_names: list[str] = []
        prior_history_shrink_reason = ''
        prior_actual_request_ref = ''
        prior_declaration_backfill: dict[str, Any] = {}
        prior_declaration_schema_ref = ''
        prior_declaration_bundle_revision = ''
        if callable(read_runtime_frame):
            prior_frame = read_runtime_frame(str(task_id or '').strip(), str(node_id or '').strip())
            if isinstance(prior_frame, dict):
                prior_trace = dict(prior_frame.get('model_visible_tool_selection_trace') or {})
                prior_selected_tool_names = [
                    str(item or '').strip()
                    for item in list(prior_frame.get('model_visible_tool_names') or [])
                    if str(item or '').strip()
                ]
                prior_provider_tool_names = [
                    str(item or '').strip()
                    for item in list(prior_frame.get('provider_tool_names') or [])
                    if str(item or '').strip()
                ]
                prior_history_shrink_reason = str(prior_frame.get('history_shrink_reason') or '').strip()
                prior_actual_request_ref = str(prior_frame.get('actual_request_ref') or '').strip()
                prior_declaration_backfill = dict(
                    prior_trace.get('declaration_schema_backfill') or {}
                )
                prior_declaration_schema_ref = str(
                    prior_trace.get('declaration_schema_ref')
                    or ''
                ).strip()
                # 单源化后整表的缓存键是 (ref, revision)；存量帧没有 revision 时读空串，
                # 读侧会退化成"每次读盘"，宁可能一次多读也不供可能过期的内容。
                prior_declaration_bundle_revision = str(
                    prior_trace.get('bundle_revision')
                    or ''
                ).strip()
        selected_tool_names: list[str] = []
        for name in prior_selected_tool_names:
            if name in model_visible_callable_tool_names and name not in selected_tool_names:
                selected_tool_names.append(name)
        for name in model_visible_callable_tool_names:
            if name not in selected_tool_names:
                selected_tool_names.append(name)
        provider_visible_tool_names = list(ordered_visible_tool_names)
        # 发给模型的 tools[] 是"按角色钉住的说明书"，不是当轮能力集：成员只在 token
        # 压缩那一跳重取（react_loop 压缩段），权限变动不改动它。能力本身由派发字典
        # 当跳放行（react_loop `_execute_tool_raw` 不校验声明），收回也在同一跳拒绝，
        # 所以清单滞后不会把能力钉死。
        pinned_provider_tool_names: list[str] = []
        for candidate_name in [
            *visible_rbac_tool_names,
            *always_callable_tool_names,
            *provider_visible_tool_names,
        ]:
            normalized_name = str(candidate_name or '').strip()
            if not normalized_name:
                continue
            if normalized_name not in pinned_provider_tool_names:
                pinned_provider_tool_names.append(normalized_name)
        if prior_provider_tool_names:
            # 钉死：非压缩跳一个名字不增删（与前门同形）。当轮新可用的名字靠尾块
            # `undeclared_candidates` 点名，调用的参数契约由 `load_tool_context`
            # 返回承载（`parameters_schema` / `parameter_contract_markdown`）；
            # 声明在 token 压缩那一跳重取（react_loop 压缩段）时一次性吸收，
            # 可复用前缀只会在重印点断。
            provider_tool_names = list(prior_provider_tool_names)
            provider_tool_bundle_mode = 'pinned_frozen'
        else:
            provider_tool_names = list(pinned_provider_tool_names)
            provider_tool_bundle_mode = 'pinned_seeded'
        # 权限收回只关执行准入，不摘清单上的名字：派发字典按当轮资格取数，名字一旦从里面
        # 消失就渲不出参数表，所以声明侧自带一份不依赖派发字典的 schema。这里的判据是
        # "这一跳能不能从派发字典渲出来"，不是"还在不在授权名单里"——水合收回、候选塌缩都会
        # 让仍被授权的名字离开派发字典，那属执行层卫生；清单排在正文最前面，删名等于把它
        # 身后全部存活正文重铺一次。资源被整体禁用（实例与快照都没有）时无从声明，只能让它
        # 离开清单。
        live_granted_tool_names = {
            name
            for name in [*visible_rbac_tool_names, *always_callable_tool_names]
        }
        dispatched_tool_names = {
            str(name or '').strip()
            for name in dict(visible_tools or {})
            if str(name or '').strip()
        }
        resource_manager = getattr(self, '_resource_manager', None)
        instance_getter = getattr(resource_manager, 'tool_instances', None)
        declared_instances = dict(instance_getter() or {}) if callable(instance_getter) else {}
        # 名字离开清单的最后一道阻力是载体：参数表的两档来源都不稳——删除前快照在进程里、
        # 重启即空，上一跳原文在工件里、会被内存/磁盘保护降级。所以凡靠原文才渲出来的名字，
        # 随 selection trace 一起落进帧（`declaration_schema_backfill`），下一跳先读载体、
        # 读不到才碰工件。装在 trace 里是刻意的：那个 dict 已整份持久化，而新增帧字段要跟
        # 七个归一化写点，漏一个就是"存了等于没存"。
        declared_denied_tool_schemas: dict[str, Any] = {}
        declaration_backfill: dict[str, Any] = {}
        declaration_placeholders: list[str] = []
        last_sent_schema_index: dict[str, Any] | None = None
        bundle_schema_index: dict[str, Any] | None = None
        # 阶段 0 观测先行：逐名记来源，并记"这一跳有多少名字需要兜底"作分母。没有分母，
        # 命中 0 分不清"这档没用"与"这档压根没机会"，阶段 4 的退役排期就没有判据。
        schema_source_hits = {
            'instance': 0,
            'retired': 0,
            'backfill': 0,
            'bundle': 0,
            'last_sent': 0,
            'placeholder': 0,
        }
        schema_fallback_opportunities = 0
        # 单源化读路径（开关 `G3KU_TOOLS_BUNDLE_SINGLE_SOURCE`，默认关）：参数表只有整表这一个
        # 来源。派发字典内的名字也一样——它们今天由 `react_loop` 现场 `to_model_schema()`，
        # 于是"同名工具被编辑"会在重印边界之外改字节；改读整表后编辑要到边界才被吸收。
        # 整表缺名（含存量第一份整表还没有后来新增的成员）时，这一跳就地按当轮可见源重建并
        # 重写整表：代价等于一次预算内的重印，而不是把名字悄悄摘掉或落回活实例。
        single_source_bundles = tools_bundle_single_source_enabled()
        bundle_index: dict[str, Any] = {}
        bundle_placeholder_names: list[str] = []
        bundle_force_reprint = False
        bundle_compat_rebuild = False
        bundle_rebuilt_in_place = False
        rebuilt_ref = ''
        rebuilt_revision = ''
        if single_source_bundles:
            bundle_index = self._pinned_tool_schema_bundle_index(
                bundle_ref=prior_declaration_schema_ref,
                bundle_revision=prior_declaration_bundle_revision,
            )
            missing_bundle_names = [
                str(name or '').strip()
                for name in provider_tool_names
                if str(name or '').strip() and str(name or '').strip() not in bundle_index
            ]
            # 兼容重建与首跳播种要分得开：前者是"整表已经有了、却缺后来新增的成员（或整表
            # 读不出来）"，每个在飞节点最多一次；后者是新节点的常态。混记会把灰度期的正常
            # 收口读成故障，也把真兼容缺口淹在播种量里。
            bundle_compat_rebuild = bool(prior_declaration_schema_ref) and (
                not bundle_index or bool(missing_bundle_names)
            )
            if not bundle_index or missing_bundle_names:
                rebuilt = self._build_node_pinned_tool_bundle(
                    task_id=str(task_id or '').strip(),
                    node_id=str(node_id or '').strip(),
                    provider_tool_names=provider_tool_names,
                    visible_tools=visible_tools,
                    prior_bundle_index=bundle_index,
                    build_reason='forced_reprint',
                    prior_index_first=bundle_compat_rebuild,
                )
                bundle_index = dict(rebuilt.get('index') or {})
                # 占位是"重建也拿不到参数表"的落点：它跟着定影进整表，并单独可数——
                # `declaration_schema_placeholders` 记的是回填链那五档全空，两件事不能混一栏。
                bundle_placeholder_names = [
                    str(name or '').strip()
                    for name in list(rebuilt.get('placeholders') or [])
                    if str(name or '').strip()
                ]
                rebuilt_ref = str(rebuilt.get('ref') or '').strip()
                rebuilt_revision = str(rebuilt.get('revision') or '').strip()
                bundle_rebuilt_in_place = bool(rebuilt_ref and bundle_index)
                if not bundle_rebuilt_in_place:
                    # 写盘失败：这一跳照旧按重建出来的字节发（下一跳还会重建），但**不退回活实例**
                    # ——回退等于把"整表说的是什么"和"线上发的是什么"再次分家。缺的名字就地
                    # 定影成占位，配一条带坐标的 WARNING 与 `bundle_fault` 计数。
                    bundle_force_reprint = True
                    logger.warning(
                        'node pinned tools[] bundle could not be persisted, serving this hop from memory: '
                        'task={} node={} names={}',
                        str(task_id or '').strip(),
                        str(node_id or '').strip(),
                        len(provider_tool_names),
                    )
                    for missing_name in provider_tool_names:
                        normalized_name = str(missing_name or '').strip()
                        if not normalized_name or normalized_name in bundle_index:
                            continue
                        bundle_index[normalized_name] = self._placeholder_pinned_tool_schema(normalized_name)
                        bundle_placeholder_names.append(normalized_name)
            else:
                for bundle_name in provider_tool_names:
                    normalized_name = str(bundle_name or '').strip()
                    if not normalized_name or normalized_name in dispatched_tool_names:
                        continue
                    schema_fallback_opportunities += 1
                    schema_source_hits['bundle'] += 1
                    declared_denied_tool_schemas[normalized_name] = bundle_index[normalized_name]
        # 五档回填链整段只在开关关闭时走：单源那支上面已经把 `declared_denied_tool_schemas`
        # 按名单填好了，这里首行 `break` 就是"一档也不进"。阶段 4 退役旧档时这段连着删。
        for candidate_name in provider_tool_names:
            if single_source_bundles:
                break
            normalized_name = str(candidate_name or '').strip()
            if not normalized_name or normalized_name in dispatched_tool_names:
                continue
            schema_fallback_opportunities += 1
            to_schema = getattr(declared_instances.get(normalized_name), 'to_model_schema', None)
            if callable(to_schema):
                declared_denied_tool_schemas[normalized_name] = to_schema()
                schema_source_hits['instance'] += 1
                continue
            # 实例已经不在（资源被删除）：取删除前抄下的那一份，`tools[]` 才能与上一跳
            # 逐字节相同。
            retired_schema = lookup_tool_schema(normalized_name)
            if isinstance(retired_schema, dict):
                declared_denied_tool_schemas[normalized_name] = retired_schema
                schema_source_hits['retired'] += 1
                continue
            carried_schema = prior_declaration_backfill.get(normalized_name)
            if isinstance(carried_schema, dict):
                declared_denied_tool_schemas[normalized_name] = carried_schema
                declaration_backfill[normalized_name] = carried_schema
                schema_source_hits['backfill'] += 1
                continue
            # 第四源：清单变化那一跳存下的整表引用。它不是"上一跳原文"——那份按 kind 被逐跳
            # 剪掉，剪掉的那一刻这档就空了；整表引用只在名单真变时重写，所以重启、剪枝、
            # 删除三者叠加也还在。
            if bundle_schema_index is None:
                bundle_schema_index = self._pinned_tool_schema_bundle_index(
                    bundle_ref=prior_declaration_schema_ref,
                    bundle_revision=prior_declaration_bundle_revision,
                )
            bundled_schema = bundle_schema_index.get(normalized_name)
            if isinstance(bundled_schema, dict):
                declared_denied_tool_schemas[normalized_name] = bundled_schema
                declaration_backfill[normalized_name] = bundled_schema
                schema_source_hits['bundle'] += 1
                continue
            # 第五源：上一跳真正发出去的那份原文（旧清单还没有整表引用时的过渡档）。原文是
            # 逐字节的历史事实，复用它不会改变 `tools[]` 的字节。取到就顺手写回载体，
            # 别让下一跳重读。
            if last_sent_schema_index is None:
                last_sent_schema_index = self._node_last_sent_tool_schema_index(
                    actual_request_ref=prior_actual_request_ref,
                )
            last_sent_schema = last_sent_schema_index.get(normalized_name)
            if isinstance(last_sent_schema, dict):
                declared_denied_tool_schemas[normalized_name] = last_sent_schema
                declaration_backfill[normalized_name] = last_sent_schema
                schema_source_hits['last_sent'] += 1
                continue
            # 五档全空：名字留在清单里，参数表明示这一跳没带出来。删名与占位在缓存上等价
            # （断点都从这一条的起始字节开始），差别全在语义——他要的是"四个来源都取不到
            # 也不许退场"。
            declared_denied_tool_schemas[normalized_name] = self._placeholder_pinned_tool_schema(
                normalized_name
            )
            schema_source_hits['placeholder'] += 1
            declaration_placeholders.append(normalized_name)
        # 尾块只点名"无权限或资源已下架"：仍被授权、只是本轮没有派发资格的名字不进这份
        # 文案——模型调它拿到的是"先 load_tool_context"那一档，措辞得跟真实处置一致。
        # 单源化后判据直接落在名单上（"在 `tools[]` 里"对整表供出的名字恒真），不再靠
        # "声明侧自带了 schema"这件事反推可渲染性；两种写法在开关关闭时同形。
        denied_source_names = (
            [str(name or '').strip() for name in provider_tool_names if str(name or '').strip()]
            if single_source_bundles
            else list(declared_denied_tool_schemas)
        )
        declared_denied_tool_names = [
            name
            for name in denied_source_names
            if name not in live_granted_tool_names
        ]
        # 载体有界：它只为"名字还在钉住清单里、参数表却只剩原文可取"那几条存东西，常态是
        # 空表。超限按名字序截断，被截掉的下一次自然回去读工件——退化成没有载体，不会退场。
        if len(declaration_backfill) > _DECLARATION_BACKFILL_MAX_ENTRIES:
            declaration_backfill = {
                name: declaration_backfill[name]
                for name in sorted(declaration_backfill)[:_DECLARATION_BACKFILL_MAX_ENTRIES]
            }
        provider_tool_membership_changed = provider_tool_names != list(prior_provider_tool_names)
        provider_tool_bundle_seeded = bool(provider_tool_membership_changed)
        # 整表引用只在名单真的变了时重写——常态跳一次 IO 都不加。写失败就沿用上一份引用并
        # 记一次可数控数：静默降级等于让"五档全空"重新变成可达。
        declaration_schema_ref = str(prior_declaration_schema_ref or '')
        declaration_bundle_revision = str(prior_declaration_bundle_revision or '')
        if bundle_rebuilt_in_place:
            # 缺名重建已经在本跳写过整表，不再重写一次。
            declaration_schema_ref = rebuilt_ref
            declaration_bundle_revision = rebuilt_revision
        declaration_schema_ref_fault = 0
        if provider_tool_bundle_mode != 'pinned_frozen' and provider_tool_names:
            built = self._build_node_pinned_tool_bundle(
                task_id=str(task_id or '').strip(),
                node_id=str(node_id or '').strip(),
                provider_tool_names=provider_tool_names,
                visible_tools=visible_tools,
                prior_bundle_index=bundle_index,
                fallback_schemas=declared_denied_tool_schemas,
                build_reason='seeded',
            )
            if built.get('ref'):
                declaration_schema_ref = str(built.get('ref') or '')
                declaration_bundle_revision = str(built.get('revision') or '')
                if built.get('index'):
                    bundle_index = dict(built.get('index') or {})
            elif not bundle_rebuilt_in_place:
                declaration_schema_ref_fault = 1
        exposure = {
            'provider_tool_names': list(provider_tool_names),
            'desired_provider_tool_names': list(pinned_provider_tool_names),
            'pending_provider_tool_names': [],
            'provider_tool_exposure_pending': False,
            'provider_tool_exposure_revision': self._provider_tool_exposure_revision(provider_tool_names),
            'provider_tool_exposure_commit_reason': '',
            'pinned_provider_tool_names': list(pinned_provider_tool_names),
            'provider_tool_bundle_mode': provider_tool_bundle_mode,
        }
        final_schema_chars = sum(
            len(json.dumps(visible_tools[name].to_model_schema(), ensure_ascii=False, sort_keys=True))
            for name in selected_tool_names
            if name in visible_tools
        )
        candidate_tool_names = [
            name
            for name in self._normalized_tool_name_list(list(getattr(selection, 'candidate_tool_names', []) or []))
            if name not in set(selected_tool_names)
        ]
        return {
            'tool_names': selected_tool_names,
            'provider_tool_names': provider_tool_names,
            'pinned_provider_tool_names': list(pinned_provider_tool_names),
            'provider_tool_bundle_mode': provider_tool_bundle_mode,
            'declared_denied_tool_names': list(declared_denied_tool_names),
            'declared_denied_tool_schemas': dict(declared_denied_tool_schemas),
            # 单源化后 wire 上那份 `tools[]` 的字节源就是这份整表索引（含派发字典内的名字），
            # 由 `react_loop` 按名单顺序取；开关关闭时它为空，渲染仍走活实例。
            'pinned_tool_bundle_index': dict(bundle_index) if single_source_bundles else {},
            'pinned_tool_bundle_ref': declaration_schema_ref,
            'pinned_tool_bundle_revision': declaration_bundle_revision,
            'candidate_tool_names': candidate_tool_names,
            'lightweight_tool_ids': list(selection.lightweight_tool_ids or []),
            'pending_provider_tool_names': list(exposure.get('pending_provider_tool_names') or []),
            'provider_tool_exposure_pending': bool(exposure.get('provider_tool_exposure_pending')),
            'provider_tool_exposure_revision': str(exposure.get('provider_tool_exposure_revision') or ''),
            'provider_tool_exposure_commit_reason': str(exposure.get('provider_tool_exposure_commit_reason') or ''),
            'schema_chars': int(final_schema_chars),
            'trace': {
                'mode': 'execution_tool_selection',
                'node_kind': normalized_node_kind,
                'session_id': session_id,
                'actor_role': actor_role,
                'rbac_visible_tool_names': list(visible_rbac_tool_names),
                'callable_tool_names': list(full_callable_tool_names),
                'full_callable_tool_names': list(full_callable_tool_names),
                'contract_present_tool_names': list(contract_present_tool_names),
                'candidate_tool_names': list(candidate_tool_names),
                'desired_provider_tool_names': list(exposure.get('desired_provider_tool_names') or []),
                'prior_provider_tool_names': list(prior_provider_tool_names),
                'prior_history_shrink_reason': str(prior_history_shrink_reason or ''),
                'provider_tool_names': list(provider_tool_names),
                'pending_provider_tool_names': list(exposure.get('pending_provider_tool_names') or []),
                'provider_tool_exposure_pending': bool(exposure.get('provider_tool_exposure_pending')),
                'provider_tool_exposure_revision': str(exposure.get('provider_tool_exposure_revision') or ''),
                'provider_tool_exposure_commit_reason': str(exposure.get('provider_tool_exposure_commit_reason') or ''),
                'provider_tool_bundle_seeded': bool(provider_tool_bundle_seeded),
                'provider_tool_bundle_mode': provider_tool_bundle_mode,
                'provider_tool_membership_changed': bool(provider_tool_membership_changed),
                'declared_denied_tool_names': list(declared_denied_tool_names),
                'base_schema_chars': int(selection.schema_chars),
                'final_schema_chars': int(final_schema_chars),
                **dict(selection.trace or {}),
                'declaration_schema_backfill': declaration_backfill,
                'declaration_schema_ref': declaration_schema_ref,
                'declaration_schema_placeholders': list(declaration_placeholders),
                'declaration_schema_ref_fault': int(declaration_schema_ref_fault),
                # 阶段 0 观测读数，装在 trace 里（整份 dict 原样持久化，零归一化写点）。
                'schema_source_hits': dict(schema_source_hits),
                'schema_source_fallback_opportunities': int(schema_fallback_opportunities),
                # 下一跳读整表要用这对值当缓存键：ref 就地覆盖后不变，revision 才随内容变。
                'bundle_ref': declaration_schema_ref,
                'bundle_revision': declaration_bundle_revision,
                # 两类重建分开计（§5.4）：`bundle_compat_rebuild` 是整表缺成员/读不出的兼容
                # 重建，`bundle_fault` 是整表写不出去（这一跳只能按内存里重建的字节发）。
                'bundle_compat_rebuild': int(bool(bundle_compat_rebuild)),
                'bundle_fault': int(bool(bundle_force_reprint) or int(declaration_schema_ref_fault or 0)),
                'bundle_placeholder_names': list(dict.fromkeys(bundle_placeholder_names)),
            },
        }

    @staticmethod
    def _should_expose_unavailable_tool(*, actor_role: str, record: Any) -> bool:
        if bool(getattr(record, 'available', True)):
            return False
        if not bool(getattr(record, 'enabled', True)):
            return False
        allowed_roles = {
            str(role or '').strip()
            for role in list(getattr(record, 'allowed_roles', []) or [])
            if str(role or '').strip()
        }
        return str(actor_role or '').strip() in allowed_roles

    @staticmethod
    def _should_expose_context_lookup_only_tool(
        *,
        actor_role: str,
        record: Any,
        decision: Any,
        tool_visible: bool,
    ) -> bool:
        if not bool(getattr(decision, 'allowed', False)):
            return False
        if tool_visible:
            return False
        if not bool(getattr(record, 'enabled', True)):
            return False
        if not bool(getattr(record, 'available', True)):
            return False
        if not bool(getattr(record, 'callable', True)):
            return False
        allowed_roles = {
            str(role or '').strip()
            for role in list(getattr(record, 'allowed_roles', []) or [])
            if str(role or '').strip()
        }
        if str(actor_role or '').strip() not in allowed_roles:
            return False
        return True

    def _visible_tool_map(self, *, actor_role: str, session_id: str) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for record in self.list_visible_tools(actor_role=actor_role, session_id=session_id):
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            if tool_id and tool_id not in mapping:
                mapping[tool_id] = record
        return mapping

    @staticmethod
    def _tool_context_session_id(*, runtime_context: dict[str, Any], task: Any) -> str:
        return str(
            dict(runtime_context or {}).get('session_key')
            or getattr(task, 'session_id', '')
            or 'web:shared'
        ).strip() or 'web:shared'

    @staticmethod
    def _tool_context_actor_role(*, runtime_context: dict[str, Any], node_kind: str) -> str:
        normalized_node_kind = str(node_kind or '').strip().lower()
        default_role = 'inspection' if normalized_node_kind == 'acceptance' else 'execution'
        return str(dict(runtime_context or {}).get('actor_role') or default_role).strip() or default_role

    @staticmethod
    def _fixed_builtin_tool_name_set_for_actor_role(actor_role: str) -> set[str]:
        return fixed_builtin_tool_name_set_for_actor_role(actor_role)

    @staticmethod
    def _provider_tool_exposure_revision(tool_names: list[str] | None) -> str:
        normalized = [
            str(item or '').strip()
            for item in list(tool_names or [])
            if str(item or '').strip()
        ]
        if not normalized:
            return ''
        payload = json.dumps(normalized, ensure_ascii=False, separators=(',', ':'))
        return f"pte:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    @classmethod
    def _refresh_provider_tool_bundle(
        cls,
        *,
        prior_provider_tool_names: list[str] | None,
        desired_provider_tool_names: list[str] | None,
        prior_history_shrink_reason: str = '',
    ) -> dict[str, Any]:
        prior = [
            str(item or '').strip()
            for item in list(prior_provider_tool_names or [])
            if str(item or '').strip()
        ]
        desired = [
            str(item or '').strip()
            for item in list(desired_provider_tool_names or [])
            if str(item or '').strip()
        ]
        prior_membership = set(prior)
        desired_membership = set(desired)
        membership_changed = prior_membership != desired_membership
        if not prior:
            active = list(desired)
        elif membership_changed:
            active = list(desired)
        else:
            # Preserve the persisted bundle exactly when membership is unchanged
            # so prompt-cache prefixes do not churn on harmless reorder noise.
            active = list(prior)
        return {
            'provider_tool_names': list(active),
            'pending_provider_tool_names': [],
            'provider_tool_exposure_pending': False,
            'provider_tool_exposure_revision': cls._provider_tool_exposure_revision(active),
            'provider_tool_exposure_commit_reason': '',
            'provider_tool_bundle_seeded': bool((not prior and active) or membership_changed),
            'desired_provider_tool_names': list(desired),
            'prior_history_shrink_reason': str(prior_history_shrink_reason or '').strip(),
            'provider_tool_membership_changed': bool(membership_changed),
        }

    @classmethod
    def _resolve_provider_tool_exposure(
        cls,
        *,
        active_provider_tool_names: list[str] | None,
        pending_provider_tool_names: list[str] | None,
        desired_provider_tool_names: list[str] | None,
        commit_reason: str = '',
    ) -> dict[str, Any]:
        _ = pending_provider_tool_names, commit_reason
        return cls._refresh_provider_tool_bundle(
            prior_provider_tool_names=active_provider_tool_names,
            desired_provider_tool_names=desired_provider_tool_names,
        )

    def _tool_context_hydration_targets(
        self,
        *,
        requested_tool_id: str,
        visible_tool: Any,
        actor_role: str = '',
    ) -> list[str]:
        requested_name = str(requested_tool_id or '').strip()
        if not requested_name or visible_tool is None:
            return []
        if requested_name != str(getattr(visible_tool, 'tool_id', '') or '').strip():
            return []
        fixed_builtin_names = self._fixed_builtin_tool_name_set_for_actor_role(actor_role)
        if requested_name in fixed_builtin_names:
            return []
        return [requested_name]

    def _tool_context_contract_payload(
        self,
        *,
        actor_role: str,
        session_id: str,
        requested_tool_id: str,
        resolved_tool_id: str,
        callable_value: Any,
        available_value: Any,
    ) -> dict[str, Any]:
        tool_key = str(requested_tool_id or '').strip() or str(resolved_tool_id or '').strip()
        visible_tool = self._visible_tool_map(actor_role=actor_role, session_id=session_id).get(tool_key)
        hydration_targets = self._tool_context_hydration_targets(
            requested_tool_id=tool_key,
            visible_tool=visible_tool,
            actor_role=actor_role,
        )
        callable_now = bool(callable_value) and bool(available_value)
        requested_or_resolved_tool_id = str(requested_tool_id or '').strip() or str(resolved_tool_id or '').strip()
        return {
            'callable_now': callable_now,
            'will_be_hydrated_next_turn': callable_now and bool(hydration_targets),
            'hydration_targets': list(hydration_targets),
            'exec_runtime_policy': (
                self._current_exec_runtime_policy_payload()
                if requested_or_resolved_tool_id == EXEC_TOOL_ID
                else None
            ),
        }

    def _current_exec_runtime_policy_payload(self) -> dict[str, Any]:
        record = self._raw_tool(EXEC_TOOL_ID)
        descriptor = (
            self._resource_manager.get_tool_descriptor(EXEC_TOOL_ID)
            if self._resource_manager is not None and hasattr(self._resource_manager, 'get_tool_descriptor')
            else None
        )
        return resolve_exec_runtime_policy_payload(
            record=record,
            descriptor=descriptor,
        )

    @staticmethod
    def _execution_control_tool_names(*, node_kind: str, can_spawn_children: bool) -> list[str]:
        normalized_node_kind = str(node_kind or '').strip().lower()
        ordered: list[str] = []
        if normalized_node_kind in {'execution', 'acceptance'}:
            ordered.append('submit_final_result')
        if normalized_node_kind == 'execution' and bool(can_spawn_children):
            ordered.append('spawn_child_nodes')
        return ordered

    def contract_present_tool_names_for_node(
        self,
        *,
        actor_role: str,
        session_id: str,
        request_messages: list[dict[str, Any]] | None,
    ) -> list[str]:
        """这一跳契约块在场、且治理放行的名字，按治理可见集的顺序返回。

        判据只读当次请求视图，没有任何跨跳台账需要回写：块不在眼前就是不在线，下一次调用
        自己会把契约带回来。`request_messages=None` 表示调用方手上没有当次请求视图（例如
        对象字典构建），按空集返回——宁可不放行，不可无契约放行。
        """
        if request_messages is None:
            return []
        index = contract_presence_index(request_messages=request_messages)
        if not index:
            return []
        visible_names = [
            str(name or '').strip()
            for name in list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
            if str(name or '').strip()
        ]
        if not visible_names:
            return sorted(index)
        return [name for name in visible_names if name in index]

    def _callable_tool_names_for_node(
        self,
        *,
        task,
        node: Any,
        visible_tool_names: list[str] | None = None,
        request_messages: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_names = self._normalized_tool_name_list(
            list(visible_tool_names or self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
        )
        visible_name_set = set(visible_names)
        callable_names: list[str] = []
        seen: set[str] = set()
        for name in self._execution_control_tool_names(
            node_kind=str(getattr(node, 'node_kind', '') or ''),
            can_spawn_children=bool(getattr(node, 'can_spawn_children', False)),
        ):
            if name in visible_name_set and name not in seen:
                callable_names.append(name)
                seen.add(name)
        for name in self._execution_fixed_builtin_tool_names(visible_tool_names=visible_names):
            if name not in seen:
                callable_names.append(name)
                seen.add(name)
        for name in self.contract_present_tool_names_for_node(
            actor_role=actor_role,
            session_id=session_id,
            request_messages=request_messages,
        ):
            if name not in seen:
                callable_names.append(name)
                seen.add(name)
        return callable_names

    def _model_visible_callable_tool_names_for_node(
        self,
        *,
        task_id: str,
        node_id: str,
        node_kind: str,
        callable_tool_names: list[str] | None,
    ) -> list[str]:
        return self._normalized_tool_name_list(list(callable_tool_names or []))

    def _candidate_tool_names_for_node(
        self,
        *,
        task,
        node: Any,
        selection: Any | None,
        visible_tool_names: list[str] | None = None,
    ) -> list[str]:
        visible_names = self._normalized_tool_name_list(
            list(visible_tool_names or self.list_effective_tool_names(
                actor_role=self._actor_role_for_node(node),
                session_id=str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared',
            ) or [])
        )
        visible_name_set = set(visible_names)
        callable_name_set = set(self._callable_tool_names_for_node(task=task, node=node, visible_tool_names=visible_names))
        candidates: list[str] = []
        for raw_name in list(getattr(selection, 'candidate_tool_names', []) or []):
            name = str(raw_name or '').strip()
            if not name or name not in visible_name_set or name in callable_name_set or name in candidates:
                continue
            candidates.append(name)
        return candidates

    @staticmethod
    def _search_limit(limit: int | None, *, default: int = 5, max_limit: int = 20) -> int:
        try:
            value = int(limit if limit is not None else default)
        except Exception:
            value = default
        return max(1, min(max_limit, value))

    @staticmethod
    def _matched_search_fields(query: str, fields: dict[str, Any]) -> list[str]:
        raw_query = ' '.join(str(query or '').lower().split())
        if not raw_query:
            return []
        terms = [term for term in re.split(r'[^\w\u4e00-\u9fff]+', raw_query) if term]
        matched: list[str] = []
        for field_name, value in dict(fields or {}).items():
            haystack = ' '.join(str(value or '').lower().split())
            if not haystack:
                continue
            if raw_query in haystack or any(term in haystack for term in terms):
                matched.append(str(field_name))
        return matched

    def _search_visible_skills(
        self,
        *,
        actor_role: str,
        session_id: str,
        search_query: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query = str(search_query or '').strip()
        if not query:
            return {'ok': False, 'error': 'search_query_required'}
        visible = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id) or [])
        scored: list[tuple[float, str, Any, list[str]]] = []
        for record in visible:
            skill_id = str(getattr(record, 'skill_id', '') or '').strip()
            display_name = str(getattr(record, 'display_name', '') or '').strip()
            description = str(getattr(record, 'description', '') or '').strip()
            score = score_query(query, skill_id, display_name, description)
            if score <= 0:
                continue
            matched_fields = self._matched_search_fields(
                query,
                {
                    'skill_id': skill_id,
                    'display_name': display_name,
                    'description': description,
                },
            )
            scored.append((score, skill_id, record, matched_fields))
        scored.sort(key=lambda item: (-item[0], item[1]))
        effective_limit = self._search_limit(limit)
        candidates = [
            {
                'skill_id': str(getattr(record, 'skill_id', '') or '').strip(),
                'display_name': str(getattr(record, 'display_name', '') or '').strip(),
                'description': str(getattr(record, 'description', '') or '').strip(),
                'match_score': score,
                'matched_fields': list(matched_fields),
            }
            for score, _skill_id, record, matched_fields in scored[:effective_limit]
        ]
        return {
            'ok': True,
            'mode': 'search',
            'query': query,
            'limit': effective_limit,
            'total_visible': len(visible),
            'candidates': candidates,
            'message': '' if candidates else 'No visible skills matched the query.',
            'next_action_hint': 'Call load_skill_context(skill_id="<skill_id>") to load details for a candidate.',
        }

    def _search_visible_tools(
        self,
        *,
        actor_role: str,
        session_id: str,
        search_query: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query = str(search_query or '').strip()
        if not query:
            return {'ok': False, 'error': 'search_query_required'}
        visible = list(self.list_visible_tools(actor_role=actor_role, session_id=session_id) or [])
        scored: list[tuple[float, int, int, str, Any, list[str]]] = []
        for record in visible:
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            display_name = str(getattr(record, 'display_name', '') or '').strip()
            description = str(getattr(record, 'description', '') or '').strip()
            score = score_query(query, tool_id, display_name, description)
            if score <= 0:
                continue
            matched_fields = self._matched_search_fields(
                query,
                {
                    'tool_id': tool_id,
                    'display_name': display_name,
                    'description': description,
                },
            )
            scored.append(
                (
                    score,
                    1 if bool(getattr(record, 'available', True)) else 0,
                    1 if bool(getattr(record, 'callable', True)) else 0,
                    tool_id,
                    record,
                    matched_fields,
                )
            )
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        effective_limit = self._search_limit(limit)
        candidates = [
            {
                'tool_id': str(getattr(record, 'tool_id', '') or '').strip(),
                'display_name': str(getattr(record, 'display_name', '') or '').strip(),
                'description': str(getattr(record, 'description', '') or '').strip(),
                'available': bool(getattr(record, 'available', True)),
                'callable': bool(getattr(record, 'callable', True)),
                'tool_type': str(getattr(record, 'tool_type', '') or '').strip(),
                'install_dir': str(getattr(record, 'install_dir', '') or '').strip() or None,
                'match_score': score,
                'matched_fields': list(matched_fields),
            }
            for score, _available_rank, _callable_rank, _tool_id, record, matched_fields in scored[:effective_limit]
        ]
        return {
            'ok': True,
            'mode': 'search',
            'query': query,
            'limit': effective_limit,
            'total_visible': len(visible),
            'candidates': candidates,
            'message': '' if candidates else 'No visible tools matched the query.',
            'next_action_hint': 'Call load_tool_context(tool_id="<tool_id>") to load details for a candidate.',
        }

    def load_skill_context(
        self,
        *,
        actor_role: str,
        session_id: str,
        skill_id: str = '',
        search_query: str = '',
        limit: int | None = None,
    ) -> dict[str, Any]:
        skill_name = str(skill_id or '').strip()
        if not skill_name:
            if str(search_query or '').strip():
                return self._search_visible_skills(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'skill_id_or_search_query_required'}
        visible = {
            item.skill_id: item
            for item in self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id)
        }
        record = visible.get(skill_name)
        if record is None:
            # 三态回执：注册表里没有 = 已删除（治理记录随资源一起清掉）；
            # 有记录但角色不可见 = 权限不允许。名单钉在头部、两次重印之间会过期，
            # 模型按 stale 名单来 load 是常态，回执必须说清"为什么读不了"。
            registry_getter = getattr(self.resource_registry, 'get_skill_resource', None)
            registry_record = registry_getter(skill_name) if callable(registry_getter) else None
            if registry_record is None:
                return {
                    'ok': False,
                    'error': 'skill_deleted',
                    'message': f'Skill 已删除：{skill_id}。名单里的这个名字已过期，不要再尝试。',
                }
            return {
                'ok': False,
                'error': 'skill_not_permitted',
                'message': f'权限不允许：角色 {actor_role} 无权读取 {skill_id}。重试或改名都不会放行。',
            }
        if self._skill_record_is_repair_required(record):
            return self._repair_required_skill_payload(record)
        path = Path(record.skill_doc_path) if record.skill_doc_path else None
        content = path.read_text(encoding='utf-8') if path and path.exists() else ''
        return {
            'ok': True,
            'skill_id': record.skill_id,
            'content': content,
        }

    def load_skill_context_v2(
        self,
        *,
        actor_role: str,
        session_id: str,
        skill_id: str = '',
        search_query: str = '',
        limit: int | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        skill_name = str(skill_id or '').strip()
        if not skill_name:
            if str(search_query or '').strip():
                return self._search_visible_skills(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'skill_id_or_search_query_required'}
        visible = {
            item.skill_id: item
            for item in self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id)
        }
        record = visible.get(skill_name)
        if record is None:
            # 三态回执：注册表里没有 = 已删除（治理记录随资源一起清掉）；
            # 有记录但角色不可见 = 权限不允许。名单钉在头部、两次重印之间会过期，
            # 模型按 stale 名单来 load 是常态，回执必须说清"为什么读不了"。
            registry_getter = getattr(self.resource_registry, 'get_skill_resource', None)
            registry_record = registry_getter(skill_name) if callable(registry_getter) else None
            if registry_record is None:
                return {
                    'ok': False,
                    'error': 'skill_deleted',
                    'message': f'Skill 已删除：{skill_id}。名单里的这个名字已过期，不要再尝试。',
                }
            return {
                'ok': False,
                'error': 'skill_not_permitted',
                'message': f'权限不允许：角色 {actor_role} 无权读取 {skill_id}。重试或改名都不会放行。',
            }
        if self._skill_record_is_repair_required(record):
            return self._repair_required_skill_payload(record)
        path = Path(record.skill_doc_path) if record.skill_doc_path else None
        content = path.read_text(encoding='utf-8') if path and path.exists() else ''
        payload = layered_body_payload(
            body=content,
            title=str(getattr(record, 'display_name', '') or ''),
            description=str(getattr(record, 'description', '') or ''),
            path=str(path) if path else '',
        )
        return {
            'ok': True,
            'skill_id': record.skill_id,
            'uri': f'{RESOURCE_URI_SCHEME}skill/{record.skill_id}',
            'level': payload['level'],
            'content': payload['content'],
            'l0': payload['l0'],
            'l1': payload['l1'],
            'path': payload['path'],
        }

    def load_tool_context(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str = '',
        search_query: str = '',
        limit: int | None = None,
    ) -> dict[str, Any]:
        tool_name = str(tool_id or '').strip()
        if not tool_name:
            if str(search_query or '').strip():
                return self._search_visible_tools(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'tool_id_or_search_query_required'}
        visible = self._visible_tool_map(actor_role=actor_role, session_id=session_id)
        if self._resource_manager is None:
            return {'ok': False, 'error': 'Resource manager unavailable'}
        toolskill = self.get_tool_toolskill(tool_name) or {}
        # 说明可读性与调用授权分开：本角色不可调用的工具，只要注册过 toolskill，
        # 仍返回正文（callable_this_turn 如实为 false）。exec 就是这类——它是节点
        # 道的常驻内置，同时带 tools/exec/toolskills；验收节点读它的契约来判断子
        # 节点的命令是否合规，旧闸门把"不可调用"误答成"没有 toolskill"。
        documentation_only = tool_name not in visible and bool(str(toolskill.get('content') or '').strip())
        if tool_name not in visible and not documentation_only:
            return {'ok': False, 'error': f'Tool not visible: {tool_id}'}
        content = str(toolskill.get('content') or '')
        resolved_tool_id = str(toolskill.get('tool_id') or tool_name)
        contract_payload = self._tool_context_contract_payload(
            actor_role=actor_role,
            session_id=session_id,
            requested_tool_id=tool_name,
            resolved_tool_id=resolved_tool_id,
            callable_value=toolskill.get('callable') if not documentation_only else False,
            available_value=toolskill.get('available') if not documentation_only else False,
        )
        warnings = list(toolskill.get('warnings') or [])
        if documentation_only:
            warnings.append('本角色不可调用该工具，本次仅返回说明正文')
        payload = {
            'ok': True,
            'tool_id': resolved_tool_id,
            'content': content,
            'tool_type': toolskill.get('tool_type'),
            'install_dir': toolskill.get('install_dir'),
            'callable': toolskill.get('callable') if not documentation_only else False,
            'available': toolskill.get('available') if not documentation_only else False,
            'documentation_only': documentation_only,
            'repair_required': bool(toolskill.get('repair_required')),
            'parameters_schema': toolskill.get('parameters_schema') or {'type': 'object', 'properties': {}, 'required': []},
            'required_parameters': list(toolskill.get('required_parameters') or []),
            'parameter_contract_markdown': str(toolskill.get('parameter_contract_markdown') or ''),
            'example_arguments': toolskill.get('example_arguments') or {},
            'warnings': warnings,
            'errors': list(toolskill.get('errors') or []),
            'exec_runtime_policy': toolskill.get('exec_runtime_policy'),
            **contract_payload,
        }
        fingerprint = build_tool_context_fingerprint(payload)
        if fingerprint:
            payload['tool_context_fingerprint'] = fingerprint
        return payload

    def load_tool_context_v2(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str = '',
        search_query: str = '',
        limit: int | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        tool_name = str(tool_id or '').strip()
        if not tool_name:
            if str(search_query or '').strip():
                return self._search_visible_tools(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'tool_id_or_search_query_required'}
        visible = self._visible_tool_map(actor_role=actor_role, session_id=session_id)
        if self._resource_manager is None:
            return {'ok': False, 'error': 'Resource manager unavailable'}
        toolskill = self.get_tool_toolskill(tool_name) or {}
        # 说明可读性与调用授权分开：本角色不可调用的工具，只要注册过 toolskill，
        # 仍返回正文（callable_this_turn 如实为 false）。见 load_tool_context。
        documentation_only = tool_name not in visible and bool(str(toolskill.get('content') or '').strip())
        if tool_name not in visible and not documentation_only:
            return {'ok': False, 'error': f'Tool not visible: {tool_id}'}
        content = str(toolskill.get('content') or '')
        resolved_tool_id = str(toolskill.get('tool_id') or tool_name)
        contract_payload = self._tool_context_contract_payload(
            actor_role=actor_role,
            session_id=session_id,
            requested_tool_id=tool_name,
            resolved_tool_id=resolved_tool_id,
            callable_value=toolskill.get('callable') if not documentation_only else False,
            available_value=toolskill.get('available') if not documentation_only else False,
        )
        layered_payload = layered_body_payload(
            body=content,
            title=str(toolskill.get('tool_id') or tool_name),
            description=str(toolskill.get('description') or ''),
            path=str(toolskill.get('path') or ''),
        )
        warnings = list(toolskill.get('warnings') or [])
        if documentation_only:
            warnings.append('本角色不可调用该工具，本次仅返回说明正文')
        payload = {
            'ok': True,
            'tool_id': resolved_tool_id,
            'uri': f'{RESOURCE_URI_SCHEME}resource/tool/{resolved_tool_id}',
            'level': layered_payload['level'],
            'content': layered_payload['content'],
            'l0': layered_payload['l0'],
            'l1': layered_payload['l1'],
            'path': layered_payload['path'],
            'tool_type': toolskill.get('tool_type'),
            'install_dir': toolskill.get('install_dir'),
            'callable': toolskill.get('callable') if not documentation_only else False,
            'available': toolskill.get('available') if not documentation_only else False,
            'documentation_only': documentation_only,
            'repair_required': bool(toolskill.get('repair_required')),
            'parameters_schema': toolskill.get('parameters_schema') or {'type': 'object', 'properties': {}, 'required': []},
            'required_parameters': list(toolskill.get('required_parameters') or []),
            'parameter_contract_markdown': str(toolskill.get('parameter_contract_markdown') or ''),
            'example_arguments': toolskill.get('example_arguments') or {},
            'warnings': warnings,
            'errors': list(toolskill.get('errors') or []),
            'exec_runtime_policy': toolskill.get('exec_runtime_policy'),
            **contract_payload,
        }
        fingerprint = build_tool_context_fingerprint(payload)
        if fingerprint:
            payload['tool_context_fingerprint'] = fingerprint
        return payload

    def node_stash_carrier(self, node_id: str) -> dict[str, Any]:
        """节点载体的读口：跳序与条目都从 `node.metadata` 里来（整份 JSON 落库）。"""
        node = self.store.get_node(str(node_id or '').strip()) if node_id else None
        metadata = getattr(node, 'metadata', None)
        return tool_arg_stash.build_carrier(
            tool_arg_stash.carrier_container(metadata if isinstance(metadata, dict) else {}),
            hop=tool_arg_stash.read_hop(metadata if isinstance(metadata, dict) else {}),
        )

    def advance_node_stash_hop(self, node_id: str) -> tuple[dict[str, Any], list[str]]:
        """每跳推进一次跳序，但**只在还有暂存条目时**推进并落盘。

        没条目就没东西要老化，白写一次节点行；有时不推进，条目就永远停在"上一跳"之外。

        顺带清掉过了严格一跳的条目（否则它们整回合留在载体里，每跳都重抄进节点 metadata），
        并把被清的名字回给调用方——那是 D15 的 `abandoned` 档：暂存过却再没被调用。清扫是
        静默的，所以必须自带一个可数落点，不然"模型为什么没用这批"永远查不到。
        """
        carrier = self.node_stash_carrier(node_id)
        container = tool_arg_stash.carrier_container(carrier)
        if not container:
            return carrier, []
        next_hop = tool_arg_stash.read_hop(carrier) + 1
        swept_container, dropped_scopes = tool_arg_stash.sweep_expired(
            container,
            current_hop=next_hop,
            present_tool_ids=None,
        )
        next_carrier = self.persist_node_stash_carrier(
            node_id,
            tool_arg_stash.build_carrier(swept_container, hop=next_hop),
        )
        return next_carrier, [tool_arg_stash.scope_tool_id(scope) for scope in dropped_scopes]

    def persist_node_stash_carrier(self, node_id: str, carrier: Any) -> dict[str, Any]:
        """写回载体。按 node_id 重读存储再改，避免把循环开局那份快照的其它键覆盖掉。"""
        normalized_node_id = str(node_id or '').strip()
        if not normalized_node_id:
            return {}
        next_carrier = tool_arg_stash.build_carrier(
            tool_arg_stash.carrier_container(carrier),
            hop=tool_arg_stash.read_hop(carrier),
        )

        def _mutate(record: Any) -> Any:
            metadata = dict(getattr(record, 'metadata', None) or {})
            metadata.pop(tool_arg_stash.STASH_CONTAINER_KEY, None)
            metadata.pop(tool_arg_stash.STASH_HOP_KEY, None)
            metadata.update(next_carrier)
            record.metadata = metadata
            return record

        updated = self.store.update_node(normalized_node_id, _mutate)
        if updated is None:
            return {}
        metadata = getattr(updated, 'metadata', None)
        return tool_arg_stash.build_carrier(
            tool_arg_stash.carrier_container(metadata if isinstance(metadata, dict) else {}),
            hop=tool_arg_stash.read_hop(metadata if isinstance(metadata, dict) else {}),
        )

    def deliver_impulse_contract(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        scope_key: str,
        carrier: Any,
        batch_position: int = 1,
        batch_total: int = 1,
        runtime_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """未水合工具被直接调用时的那一跳：交契约、暂存入参、**不执行**（D3）。

        两条车道都调这里，所以回执文本、校验口径、暂存规则只有一份。三种结果：

        - `staged`：入参校验通过 ⇒ 落 stash、带契约块与 `reuse_args` 提示，本轮不占预算；
        - `invalid`：校验失败 ⇒ 带契约块与参数错误，不落 stash，占一轮；
        - `reject`：这不是"没水合"这一类（名字无权限、资源下架、repair-required、压根没
          这个名字）⇒ 空内容返回，调用方保留自己原有的拒绝回执。

        跳序从载体里读，不接受调用方传 hop：时钟与条目必须同刻，让外部传一个数字就是给
        "条目永远等不到自己那一跳"留口子。
        """
        normalized_tool = str(tool_id or '').strip()
        hop = tool_arg_stash.read_hop(carrier)
        rejected: dict[str, Any] = {
            'outcome': IMPULSE_REJECT,
            'content': '',
            'carrier': carrier if isinstance(carrier, dict) else {},
            'staged_count': 0,
            'executed': False,
        }
        if not normalized_tool:
            return rejected
        visible = self._visible_tool_map(actor_role=actor_role, session_id=session_id)
        if normalized_tool not in visible:
            return rejected
        instance = self._resource_manager.get_tool(normalized_tool) if self._resource_manager is not None else None
        if instance is None:
            return rejected
        payload = self.get_tool_toolskill(normalized_tool)
        if not isinstance(payload, dict) or bool(payload.get('repair_required')):
            return rejected
        fingerprint = build_tool_context_fingerprint(payload)
        if not fingerprint:
            return rejected
        block = render_tool_contract({**payload, 'tool_context_fingerprint': fingerprint})
        if not block:
            return rejected

        pending_container = tool_arg_stash.carrier_container(carrier)
        # 份数是**整批**的属性，不是这一条的：校验失败的那条也要能把"这批最终几份"报出来，
        # 否则末条恰好失败时，模型只看到前几条的 `set i < n`，拼不出会跑多少。
        consumable_count = len(
            tool_arg_stash.peek_entries(
                pending_container,
                scope_key=scope_key,
                current_hop=hop + 1,
                fingerprint=fingerprint,
            )
        )

        # 暂存这一道也要先剥壳：契约投递时校验的、和下一跳 `reuse_args` 真执行的，必须是
        # 同一份形状，否则"校验放行的是文本、跑的是容器"这种分家又回来了。
        arguments, _coerced_paths = instance.coerce_arguments(arguments)
        try:
            validation_errors = instance.validate_params(arguments)
        except Exception as exc:
            validation_errors = None
            guidance = append_parameter_error_guidance(
                f'Error validating {normalized_tool}: {exc}',
                tool_name=normalized_tool,
                tool=instance,
                arguments=arguments,
                runtime_context=runtime_context,
            )
            receipt = impulse_receipt(
                tool_name=normalized_tool,
                contract_block=block,
                outcome=IMPULSE_INVALID,
                staged_count=consumable_count,
                batch_position=batch_position,
                batch_total=batch_total,
                parameter_error_text=guidance,
            )
            return {
                'outcome': IMPULSE_INVALID,
                'content': receipt['content'],
                'carrier': carrier if isinstance(carrier, dict) else {},
                'staged_count': 0,
                'executed': False,
            }
        if validation_errors:
            guidance = append_parameter_error_guidance(
                'Error: ' + '; '.join(validation_errors),
                tool_name=normalized_tool,
                tool=instance,
                arguments=arguments,
                runtime_context=runtime_context,
            )
            receipt = impulse_receipt(
                tool_name=normalized_tool,
                contract_block=block,
                outcome=IMPULSE_INVALID,
                staged_count=consumable_count,
                batch_position=batch_position,
                batch_total=batch_total,
                parameter_error_text=guidance,
            )
            return {
                'outcome': IMPULSE_INVALID,
                'content': receipt['content'],
                'carrier': carrier if isinstance(carrier, dict) else {},
                'staged_count': 0,
                'executed': False,
            }

        pending_container = tool_arg_stash.carrier_container(carrier)
        existing_digests = {
            str(entry.get(tool_arg_stash.DIGEST_FIELD) or '').strip()
            for entry in tool_arg_stash.peek_entries(
                pending_container,
                scope_key=scope_key,
                current_hop=hop + 1,
                fingerprint=fingerprint,
            )
        }
        # 同一跳里签名完全相同的重复调用只入一条（D5）：否则 `reuse_args=true` 会把同一份
        # 入参执行两遍，而模型看到的两份回执是分开的两条消息，它没有理由知道要合并。
        if tool_arg_stash.args_digest(arguments) not in existing_digests:
            pending_container, _entry, _staged_total = tool_arg_stash.stage_args(
                pending_container,
                scope_key=scope_key,
                args=arguments,
                fingerprint=fingerprint,
                staged_at=now_iso(),
                staged_hop=hop,
            )
        # 份数只数**下一跳真会被执行**的条目：容器里可能还留着上一跳过期或别的修订的条目，
        # 按 list 长度报会把永远不会跑的那些算进去（`staged_count` 就是长度，别用它报数）。
        staged_count = len(
            tool_arg_stash.peek_entries(
                pending_container,
                scope_key=scope_key,
                current_hop=hop + 1,
                fingerprint=fingerprint,
            )
        )
        receipt = impulse_receipt(
            tool_name=normalized_tool,
            contract_block=block,
            outcome=IMPULSE_STAGED,
            staged_count=staged_count,
            batch_position=batch_position,
            batch_total=batch_total,
        )
        return {
            'outcome': IMPULSE_STAGED,
            'content': receipt['content'],
            'carrier': tool_arg_stash.build_carrier(pending_container, hop=hop),
            'staged_count': staged_count,
            'fingerprint': fingerprint,
            'executed': False,
        }

    def list_skill_resources(self) -> list[Any]:
        return list(self.resource_registry.list_skill_resources())

    def get_skill_resource(self, skill_id: str):
        return self.resource_registry.get_skill_resource(str(skill_id or '').strip())

    def list_skill_files(self, skill_id: str) -> dict[str, str]:
        return {key: str(path) for key, path in self.resource_registry.skill_file_map(str(skill_id or '').strip()).items()}

    def read_skill_file(self, skill_id: str, file_key: str) -> str:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        return path.read_text(encoding='utf-8')

    def capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'capture_resource_tree_state'):
            return {}
        return manager.capture_resource_tree_state()

    def refresh_resource_paths(
        self,
        paths: list[str | Path],
        *,
        trigger: str = 'path-change',
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        manager = getattr(self, '_resource_manager', None)
        registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if manager is not None and hasattr(manager, 'refresh_paths'):
            manager.refresh_paths(list(paths or []), trigger=trigger)
            if registry is not None and hasattr(registry, 'refresh_from_current_resources'):
                skills, tools = registry.refresh_from_current_resources()
                if policy_engine is not None and hasattr(policy_engine, 'sync_default_role_policies'):
                    policy_engine.sync_default_role_policies()
                self._record_resource_tree_state()
                return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}
        fallback = getattr(self, 'reload_resources', None)
        if callable(fallback):
            return fallback(session_id=session_id)
        return {'ok': True, 'session_id': session_id, 'skills': 0, 'tools': 0}

    def refresh_changed_resources(
        self,
        before_state: dict[str, dict[str, str]] | None = None,
        *,
        trigger: str = 'path-change',
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        manager = getattr(self, '_resource_manager', None)
        registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if manager is not None and hasattr(manager, 'refresh_changed_tree_state'):
            # 基线归服务侧：调用方（exec 工具）只报"命令跑完了"，不带自己的快照，
            # 所以一条命令只付一次全树扫描。拿上一次记下的状态当 before，检测力只增
            # 不减——命令间隙里发生的外部编辑同样会在这一轮被抓到。
            # 没有基线时只记不比：磁盘状态还在，下一次扫描会补上。
            baseline = dict(before_state or {}) or dict(getattr(self, '_resource_tree_state_cache', None) or {})
            after_state = self._resource_tree_state_snapshot()
            if baseline:
                manager.refresh_changed_tree_state(baseline, trigger=trigger, after_state=after_state)
            if registry is not None and hasattr(registry, 'refresh_from_current_resources'):
                skills, tools = registry.refresh_from_current_resources()
                if policy_engine is not None and hasattr(policy_engine, 'sync_default_role_policies'):
                    policy_engine.sync_default_role_policies()
                self._record_resource_tree_state(after_state)
                return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}
        fallback = getattr(self, 'reload_resources', None)
        if callable(fallback):
            return fallback(session_id=session_id)
        return {'ok': True, 'session_id': session_id, 'skills': 0, 'tools': 0}

    def write_skill_file(self, skill_id: str, file_key: str, content: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ''), encoding='utf-8')
        self.refresh_resource_paths([path], trigger='skill-file-write', session_id=session_id)
        return {'skill_id': str(skill_id or '').strip(), 'file_key': str(file_key or '').strip(), 'path': str(path)}

    async def write_skill_file_async(
        self,
        skill_id: str,
        file_key: str,
        content: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        item = self.write_skill_file(skill_id, file_key, content, session_id=session_id)
        item.update(await self._sync_catalog_targets(skill_ids={str(skill_id or '').strip()}))
        return item

    def _workspace_root(self) -> Path:
        if self._workspace_root_override is not None:
            return self._workspace_root_override
        manager = getattr(self, '_resource_manager', None)
        workspace = getattr(manager, 'workspace', None)
        return Path(workspace).resolve(strict=False) if workspace is not None else Path.cwd().resolve()

    @staticmethod
    def _safe_task_dir_name(task_id: str) -> str:
        return str(task_id or '').strip().replace(':', '_').replace('/', '_').replace('\\', '_')

    def _temp_root(self, *, create: bool = False) -> Path:
        """任务临时目录根：默认跟随数据根，显式 workspace_root（测试/嵌入式）优先。"""
        if self._workspace_root_override is not None:
            path = self._workspace_root_override / 'temp'
        else:
            path = data_work_path('temp')
        return ensure_dir(path) if create else path

    def _task_temp_root(self, *, create: bool = True) -> Path:
        root = self._temp_root() / 'tasks'
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _task_temp_dir(self, task_id: str, *, create: bool = True) -> Path:
        path = self._task_temp_root(create=create) / self._safe_task_dir_name(task_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _task_file_dir_path(self, task_id: str) -> Path:
        return Path(self.file_store.base_dir) / self._safe_task_dir_name(task_id)

    def _task_artifact_dir(self, task_id: str) -> Path:
        return Path(self.artifact_store._task_dir(task_id))

    def _task_event_history_dir(self, task_id: str) -> Path:
        return Path(self.store._event_history_dir) / self._safe_task_dir_name(task_id)

    def _effective_task_temp_dir(self, task_id: str) -> Path:
        runtime_meta = self.log_service.read_task_runtime_meta(task_id) or {}
        configured = str(runtime_meta.get('task_temp_dir') or '').strip()
        legacy_temp_root = self._temp_root().resolve(strict=False)
        # 默认 meta 兜底值是 temp 根目录（非每任务子目录）——等于它说明无真实
        # 配置（如 meta 行缺失），不得当每任务目录用（否则对账/删除会作用到
        # 整个 temp 根；正常任务创建时写入的一定是每任务子目录）。
        fallback_meta_root: Path | None = None
        try:
            fallback_meta_root = Path(self.log_service._fallback_task_temp_dir()).resolve(strict=False)
        except Exception:
            fallback_meta_root = None
        if configured:
            configured_path = Path(configured).expanduser().resolve(strict=False)
            if configured_path != legacy_temp_root and configured_path != fallback_meta_root:
                return configured_path
        return self._task_temp_dir(task_id, create=False)

    @classmethod
    def _remove_directory_tree_if_empty(cls, path: Path) -> bool:
        if not path.exists() or not path.is_dir():
            return False
        try:
            children = list(path.iterdir())
        except OSError:
            return False
        for child in children:
            if child.is_symlink() or not child.is_dir():
                return False
            cls._remove_directory_tree_if_empty(child)
        try:
            if any(path.iterdir()):
                return False
        except OSError:
            return False
        try:
            path.rmdir()
        except OSError:
            return False
        return True

    def _cleanup_terminal_task_temp_dir_if_empty(self, task: TaskRecord) -> None:
        task_id = str(getattr(task, 'task_id', '') or '').strip()
        if not task_id:
            return
        # 默认不自动清理任务临时目录（含空目录）：temp/tasks/<id> 在终态后
        # 原样保留，仅当显式开启 terminal_temp_dir_cleanup_enabled 才回收空壳。
        if not disk_policies().terminal_temp_dir_cleanup_enabled:
            return
        try:
            self._remove_directory_tree_if_empty(self._effective_task_temp_dir(task_id))
        except Exception:
            return

    # ------------------------------------------------------------------
    # 磁盘治理（P0-3）：任务终态即清「确定不再使用的数据」。
    # 保留清单（唯一权威）：kind=='patch'、kind=='final_output'、
    # task.final_output_ref 指向的 artifact、标题含 report/summary；
    # error_logs 表、节点 blocking_reason、task_events 行一律不动。
    # 终态即删（确定无读者）：中间产物 artifact、event-history/<task>/
    # 单份 live.patch 快照（SSE 走内存、任务树恢复帧已在终态转换时清空）、
    # task_runtime_frames 行（log_service 终态转换时清零）。
    # temp/tasks/<id> 任务临时目录终态后默认原样保留（仅当
    # terminal_temp_dir_cleanup_enabled=true，即环境变量
    # G3KU_TERMINAL_TEMP_DIR_CLEANUP_ENABLED 开启时，才随终态清理硬删）；
    # 用户删任务时仍走 delete_task 全删链路（含该目录兜底回收）。
    # 除终态清理外不再有任何自动删除：任务只随用户/模型工具手动删除而
    # 彻底清除（_wipe_task_data），磁盘紧张时靠大小可视化引导手动清理。
    # ------------------------------------------------------------------

    _TERMINAL_CLEANUP_KEEP_TITLE_TOKENS = ('report', 'summary')

    def _cleanup_terminal_task_intermediates(self, task: TaskRecord) -> None:
        """终态监听器：同步部分只做判定 + in-flight 去重 + 甩后台 daemon 线程。

        监听器同步运行在任务状态更新路径（log_service._notify_task_terminal），
        清理重活绝不能阻塞 refresh_task_view。
        """
        try:
            if not disk_policies().terminal_cleanup_enabled:
                return
            task_id = str(getattr(task, 'task_id', '') or '').strip()
            status = str(getattr(task, 'status', '') or '').strip().lower()
            if not task_id or status not in {'success', 'failed'}:
                return
            with self._terminal_cleanup_inflight_guard:
                if task_id in self._terminal_cleanup_inflight:
                    return
                self._terminal_cleanup_inflight.add(task_id)
        except Exception:
            return
        worker = threading.Thread(
            target=self._run_terminal_intermediate_cleanup_safely,
            args=(task_id,),
            name=f'task-intermediate-cleanup:{task_id}',
            daemon=True,
        )
        worker.start()

    def _run_terminal_intermediate_cleanup_safely(self, task_id: str) -> None:
        try:
            self._run_terminal_intermediate_cleanup(task_id)
        except Exception:
            return
        finally:
            try:
                with self._terminal_cleanup_inflight_guard:
                    self._terminal_cleanup_inflight.discard(task_id)
            except Exception:
                pass

    def _terminal_artifact_keep_policy(self, task: TaskRecord | None, record: TaskArtifactRecord) -> bool:
        """纯判定：该 artifact 是否属于终态后必须保留的核心内容。"""
        kind = str(getattr(record, 'kind', '') or '').strip()
        # node_input 是 `nodes.input` 外置后的唯一正文副本（行内只剩 envelope 摘要），
        # 删掉就等于把已交付节点的输入正文一起清没——终态后节点详情与恢复取证仍要读它。
        if kind in {'patch', 'final_output', 'node_input'}:
            return True
        final_ref = str(getattr(task, 'final_output_ref', '') or '').strip() if task is not None else ''
        if final_ref:
            ref_id = final_ref.split(':', 1)[1] if ':' in final_ref else final_ref
            if ref_id and str(getattr(record, 'artifact_id', '') or '').strip() == ref_id.strip():
                return True
        title = str(getattr(record, 'title', '') or '').strip().lower()
        return any(token in title for token in self._TERMINAL_CLEANUP_KEEP_TITLE_TOKENS)

    def _run_terminal_intermediate_cleanup(self, task_id: str) -> None:
        task = self.get_task(task_id)
        removed_files = 0
        removed_bytes = 0
        # a) 任务临时目录硬删（temp/tasks/<id>；remove_tree 只读文件强删，残留显式告警）。
        #    默认关闭：终态不再自动删除任务临时目录（terminal_temp_dir_cleanup_enabled=False），
        #    目录与其中内容原样保留，避免误写入 temp 系目录的正式产物因自动清理丢失。
        if disk_policies().terminal_temp_dir_cleanup_enabled:
            try:
                temp_dir = self._effective_task_temp_dir(task_id)
                if temp_dir.exists():
                    remove_tree(temp_dir)
            except Exception:
                pass
        # b) 中间 artifact：删文件 + 删 DB 行（线程内重读列表，规避入队后的竞态）
        delete_ids: list[str] = []
        try:
            artifacts = self.list_artifacts(task_id)
        except Exception:
            artifacts = []
        for artifact in artifacts:
            if self._terminal_artifact_keep_policy(task, artifact):
                continue
            artifact_id = str(getattr(artifact, 'artifact_id', '') or '').strip()
            if artifact_id:
                delete_ids.append(artifact_id)
            raw_path = str(getattr(artifact, 'path', '') or '').strip()
            if not raw_path:
                continue
            try:
                path = Path(raw_path)
                if path.is_file():
                    removed_bytes += int(path.stat().st_size)
                    path.unlink()
                    removed_files += 1
            except OSError:
                pass
        if delete_ids:
            try:
                self.store.delete_artifacts_by_ids(delete_ids)
            except Exception:
                pass
            # 同步清 artifact_store 内存去重索引，防止陈旧命中
            try:
                deleted_set = set(delete_ids)
                content_index = getattr(self.artifact_store, '_content_index', None)
                if isinstance(content_index, dict):
                    stale_keys = [
                        key for key, value in list(content_index.items())
                        if str(getattr(value, 'artifact_id', '') or '').strip() in deleted_set
                    ]
                    for key in stale_keys:
                        content_index.pop(key, None)
            except Exception:
                pass
        # c) event-history/<task>/ 目录（live.patch 单份快照）：终态后确定
        # 无运行时读者（SSE 走内存、详情走 DB），整目录删除；迟到补丁重建
        # 目录是单文件覆盖写、无害，用户删除时 wipe 兜底回收。
        try:
            remove_tree(self._task_event_history_dir(task_id))
        except Exception:
            pass
        # d) 清理量可观测
        if removed_files or delete_ids:
            logger.info(
                'disk governance: task {} intermediates cleaned files={} bytes~{} rows={}',
                task_id,
                removed_files,
                removed_bytes,
                len(delete_ids),
            )
            try:
                self.log_service.append_task_event(
                    task_id=task_id,
                    session_id=str(getattr(task, 'session_id', '') or 'web:shared'),
                    event_type='task.intermediates.cleaned',
                    data={
                        'removed_files': removed_files,
                        'removed_bytes_approx': removed_bytes,
                        'removed_rows': len(delete_ids),
                    },
                )
            except Exception:
                pass
        # e) 磁盘治理（P1）：终态即时对账（清理后的目录实测值覆盖增量记账）。
        # best-effort——对账失败不得影响清理主流程。
        try:
            self._reconcile_task_disk_usage(task_id)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 磁盘治理（P1）：紧急态钩子 / 自动暂停 / 任务大小对账。
    # 紧急线 = max(emergency_min_bytes, total*emergency_min_ratio)，跌破后：
    # 新工具调用在预算队列排队（controller target_limit=0，不拒绝）、运行中任务
    # 被自动持久暂停（排队 waiter 以 TaskPausedError 唤醒，防死锁）、告警进
    # worker_status（前端横幅/性能条）与全局事件流。空间恢复后解除硬闸，
    # 任务保持 paused 等手动 resume。
    # ------------------------------------------------------------------

    def _schedule_loop_task(self, coro_factory: Callable[[], Any]) -> None:
        """线程安全：从任意线程（monitor 采样线程）把协程调度回运行时事件循环。"""
        loop = getattr(self, '_event_loop', None)
        if loop is None:
            return
        try:
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(lambda: asyncio.create_task(coro_factory()))
        except RuntimeError:
            return

    async def _auto_pause_all_due_to_disk_emergency(self) -> None:
        policies = disk_policies()
        async with self._disk_emergency_pause_lock:
            paused_ids: list[str] = []
            if policies.auto_pause_enabled:
                try:
                    tasks = await asyncio.to_thread(self.store.list_tasks)
                except Exception:
                    tasks = []
                for task in tasks or []:
                    if str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
                        continue
                    task_id = str(getattr(task, 'task_id', '') or '').strip()
                    if not task_id:
                        continue
                    try:
                        await self.force_pause_task_durably(task_id)
                        self._abort_queued_waits(task_id)
                        paused_ids.append(task_id)
                    except Exception:
                        continue
            await self._emit_disk_emergency_alert(active=True, paused_task_ids=paused_ids)

    async def _disk_emergency_released(self) -> None:
        await self._emit_disk_emergency_alert(active=False, paused_task_ids=[])

    def _abort_queued_waits(self, task_id: str) -> None:
        """任务暂停生效后，唤醒其在预算队列中排队的工具调用（防死锁）。"""
        controller = getattr(self, 'adaptive_tool_budget_controller', None)
        abort = getattr(controller, 'abort_task_waiters', None)
        if not callable(abort):
            return
        try:
            abort(task_id, TaskPausedError(task_id))
        except Exception:
            pass

    async def _emit_disk_emergency_alert(self, *, active: bool, paused_task_ids: list[str]) -> None:
        if active and self._disk_emergency_alerted:
            return
        self._disk_emergency_alerted = bool(active)
        if active:
            logger.warning(
                'disk emergency: free space below emergency line; auto-paused {} task(s): {}',
                len(paused_task_ids),
                ','.join(paused_task_ids[:10]) or '-',
            )
        else:
            logger.info('disk emergency released; paused tasks await manual resume')
        try:
            self.log_service.append_task_event(
                task_id=None,
                session_id='web:shared',
                event_type='runtime.disk_emergency',
                data={
                    'active': bool(active),
                    'paused_task_ids': list(paused_task_ids),
                    'worker_id': self.worker_id or '',
                },
            )
        except Exception:
            pass

    async def _task_disk_reconcile_loop(self) -> None:
        """每小时对进行中任务做目录实测对账（终态任务在终态清理时对账一次；
        已终态的历史任务目录不再变化，不进小时级全扫——禁止高频 rglob 全量遍历）。
        同宿主顺带跑 P3 终态大行裁剪（23h 间隔卡权，紧急水位跳过）。"""
        while True:
            try:
                await asyncio.sleep(3600.0)
                tasks = await asyncio.to_thread(self.store.list_tasks)
                for task in tasks or []:
                    if str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
                        continue
                    task_id = str(getattr(task, 'task_id', '') or '').strip()
                    if not task_id:
                        continue
                    await asyncio.to_thread(self._reconcile_task_disk_usage, task_id)
                await self._run_detail_retention_if_due()
                await self._run_delete_ledger_sweep_if_due()
                await self._run_console_log_cap_if_due()
                await self._run_artifact_hash_backfill_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _terminal_outbox_reconcile_loop(self) -> None:
        """终态 outbox 的周期重投（60s）。

        原宿主是分发对账循环；epoch 子系统下线后只保留这一半：滞留的终态行若只在
        进程启动时重放，一次投递失败就会把结果压到下次重启（2026-09-23 实盘两条分别
        压了 1h39m 与 10h16m）。
        """
        while True:
            await asyncio.sleep(60.0)
            try:
                self._schedule_pending_task_terminal_callbacks()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _run_detail_retention_if_due(self) -> None:
        """P3：终态任务大行裁剪（默认停用：detail_retention_days<=0 直接返回；
        配置 >0 时按天裁剪，跨进程 maintenance_runs 卡权 + 紧急水位跳过）。"""
        policies = disk_policies()
        retention_days = int(policies.detail_retention_days)
        if retention_days <= 0:
            return
        if self._disk_emergency_due():
            return  # 裁剪自身是写放大源，紧急态不跑
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec='seconds')
        try:
            claimed = await asyncio.to_thread(
                self.store.claim_maintenance_run,
                'detail_prune',
                min_interval_seconds=23 * 3600.0,
                detail=f'cutoff={cutoff}',
            )
        except Exception:
            return
        if not claimed:
            return
        try:
            result = await asyncio.to_thread(self.store.prune_task_detail_rows, cutoff)
        except Exception:
            return
        task_count = int(result.get('task_count') or 0)
        deleted_total = sum(int(v or 0) for v in (result.get('deleted') or {}).values())
        if task_count or deleted_total:
            logger.info(
                'disk governance: detail retention pruned tasks={} rows={} (cutoff={})',
                task_count,
                deleted_total,
                cutoff,
            )

    async def _run_artifact_hash_backfill_if_due(self) -> None:
        """给没有 `content_hash` 的存量 artifact 行补哈希（24 h 一趟，跨进程卡权）。

        不补就要付两次代价：`_legacy_content_hash` 只把哈希记在进程内、不回写库，所以
        每次重启后的第一次正文查重要把那批文件整个读回来算一遍——实盘 984 行 ≈ 2.2 s，
        长块榜上是 `fetchall:artifacts[from=_find_existing_text_artifact]` 2,246.9 / 2,326.4 ms，
        对得上 09:36:42 那拍 2,519 ms 的事件循环滞后。补进 payload 后查重只剩 SQL 比较。
        """
        try:
            claimed = await asyncio.to_thread(
                self.store.claim_maintenance_run,
                'artifact_content_hash_backfill',
                min_interval_seconds=24 * 3600.0,
            )
        except Exception:
            return
        if not claimed:
            return
        try:
            filled = await asyncio.to_thread(
                self.artifact_store.backfill_missing_content_hashes, limit=200, batches=20
            )
            remaining = await asyncio.to_thread(self.store.count_artifacts_missing_content_hash)
        except Exception as exc:
            logger.warning('artifact content hash backfill failed: {}', exc)
            return
        logger.info(
            'artifact content hash backfill: filled={} remaining={}',
            int(filled or 0),
            int(remaining or 0),
        )

    def _reconcile_task_disk_usage(self, task_id: str) -> None:
        """对账口径 = 目录实测（files/artifacts/event-history/temp）+ 数据库
        明细字节（五张大行表 payload）。DB 部分失败时回落纯目录值，绝不抛。

        DB 分量走锚点：小时级常态只读两张大表水位之后的新行（实盘最近 2,000 行 = 13.45 MB /
        35.7 ms，整任务重扫是 367.24 MB / 冷页 55,602 ms），三张小表整任务重扫（45 ms 量级）。
        锚点在三种情况下重建：没有锚点、锚点超过 24 h、大表行数减少（裁剪或删除）。
        盲区只有一条：同一行被 ON CONFLICT 原地改写，既不改 rowid 也不改行数 ⇒ 最长 24 h 后归零。
        """
        normalized_task_id = self.normalize_task_id(task_id)
        try:
            total = self._task_disk_usage_bytes(normalized_task_id)
        except Exception:
            return
        anchor: dict[str, Any] | None = None
        db_bytes = 0
        mode = 'fallback'
        try:
            anchor = self.store.read_task_disk_anchor(normalized_task_id)
            db_bytes, anchor, mode = self.store.reconcile_task_detail_bytes(normalized_task_id, anchor)
            total += int(db_bytes or 0)
        except Exception:
            anchor = None
        try:
            self.store.upsert_task_disk_usage(normalized_task_id, int(total or 0), anchor=anchor)
        except Exception:
            return
        if anchor:
            last = dict(anchor.get('last') or {})
            logger.info(
                'disk reconcile: task={} mode={} db_bytes={:.2f} MB new_rows={} elapsed_ms={}',
                normalized_task_id,
                mode,
                int(db_bytes or 0) / 1e6,
                last.get('scanned_rows'),
                last.get('elapsed_ms'),
            )

    # ------------------------------------------------------------------
    # 磁盘治理：任务删除全量清除。
    # 治理阶梯：终态清理（P0：中间产物 + 确定不再使用的快照数据）→ 手动删除
    # （用户 Web 操作 / 模型删除工具）。磁盘紧张不再自动删除任何任务——由
    # 任务大小可视化与排序引导手动清理；紧急线只做暂停/限流保护。
    # zip 归档/压缩渐进/清理线全删渐进/pin/墓碑已整体移除（历史回放需求由
    # 单份最新快照 + 离散事件 slim 行 + DB 明细承担）。_wipe_task_data 为
    # 手动删除共用的清除核心，步骤顺序（S0-S7）是契约。
    # ------------------------------------------------------------------

    # 删除台账守卫窗口（天，模块常量）：过期前做最终幂等补偿再删台账行，
    # 台账自身不允许成为新的"无条件永久保留"。
    _LEDGER_RETENTION_DAYS = 7

    async def _wipe_task_data(self, task: TaskRecord, *, reason: str) -> dict[str, Any]:
        """任务删除全量清除核心（用户删除 / 模型工具删除 / 墓碑清扫共用）。

        步骤顺序是契约：
        S0 路径快照（_effective_task_temp_dir 读 runtime_meta，必须在删 DB 前取）
        S1 导出产出（失败 warning 不阻断；此时未写台账未删任何东西，可安全中止）
        S2 写台账（迟到写守卫自此刻生效；崩溃后凭 wiped=0 由 sweep 补偿）
        S3 删文件（artifacts/files/event-history/temp 双路径，逐项容错）
        S4 删 DB 行（23 张任务作用域表）+ shutdown-pause 台账退役
        S5 governance 审批行（跨库无原子性，失败由 sweep 补偿重放）
        S6 台账 wiped=1 + task.deleted 推送 + 会话级审计事件
        S7 内存缓存清理（summary 字典 / inflight 集合 / log_service / registry）
        """
        task_id = str(task.task_id or '').strip()
        stats: dict[str, Any] = {'task_id': task_id, 'reason': str(reason or ''), 'deliverables_exported': 0}
        # S0：temp 双路径必须在 store.delete_task 删掉 runtime_meta 行之前读取。
        temp_paths = list(dict.fromkeys((
            self._effective_task_temp_dir(task_id),
            self._task_temp_dir(task_id, create=False),
        )))
        # S1：导出产出（报告等，判据=终态保留清单）。
        try:
            export = self._export_task_deliverables(task, reason=reason)
            stats['deliverables_exported'] = int(export.get('exported') or 0)
            stats['deliverables_dir'] = str(export.get('dir') or '')
        except Exception:
            logger.warning('task wipe: deliverables export failed for {}', task_id, exc_info=True)
        # S2：写台账。
        try:
            await asyncio.to_thread(
                self.store.record_task_delete,
                task_id,
                reason=str(reason or ''),
                deliverables_exported=int(stats.get('deliverables_exported') or 0),
            )
        except Exception:
            logger.warning('task wipe: ledger record failed for {}', task_id, exc_info=True)
        # S3：删文件。
        await asyncio.to_thread(self._wipe_task_files, task_id, temp_paths)
        # S4：删 DB 行 + shutdown-pause 台账退役（防止重启后凭旧台账复活暂停态）。
        await asyncio.to_thread(self.store.delete_task, task_id)
        try:
            await asyncio.to_thread(self.store.mark_shutdown_pause_entry_consumed, kind='task', ref_id=task_id)
        except Exception:
            pass
        # S5：governance 审批行（含命令明文）。
        stats['governance_rows'] = await asyncio.to_thread(self._wipe_governance_rows, task_id)
        # S6：完成标记 + 事件。
        try:
            await asyncio.to_thread(self.store.mark_task_delete_wiped, task_id)
        except Exception:
            pass
        self._publish_task_deleted_event(session_id=task.session_id, task_id=task_id)
        try:
            # 会话级审计事件（task_id=None 绕开台账守卫；先例：disk_emergency 事件）。
            self.log_service.append_task_event(
                task_id=None,
                session_id=str(task.session_id or 'web:shared'),
                event_type='runtime.task_wiped',
                data={
                    'task_id': task_id,
                    'reason': str(reason or ''),
                    'deliverables_exported': int(stats.get('deliverables_exported') or 0),
                },
            )
        except Exception:
            pass
        logger.info(
            'task wipe: {} fully deleted (reason={}, deliverables_exported={}, governance_rows={})',
            task_id,
            reason,
            int(stats.get('deliverables_exported') or 0),
            int(stats.get('governance_rows') or 0),
        )
        # S7：内存缓存。
        self._discard_task_memory_caches(task_id)
        try:
            await self.registry.forget_task(task.session_id, task_id)
        except Exception:
            pass
        return stats

    def _wipe_task_files(self, task_id: str, temp_paths: list[Path]) -> None:
        """S3 文件级删除：artifacts / files / event-history / temp 双路径，逐项容错。

        单点失败不中止——残留由台账 sweep 的 _compensate_wipe 幂等补收。
        """
        try:
            artifacts = self.list_artifacts(task_id)
            self.artifact_store.delete_artifacts_for_task(task_id, artifacts=artifacts)
        except Exception:
            logger.warning('task wipe: artifact delete failed for {}', task_id, exc_info=True)
        try:
            self.file_store.delete_task_files(task_id)
        except Exception:
            pass
        try:
            remove_tree(self._task_event_history_dir(task_id))
        except Exception:
            pass
        for temp_path in temp_paths:
            try:
                remove_tree(temp_path)
            except Exception:
                pass

    def _wipe_governance_rows(self, task_id: str) -> int:
        try:
            return int(self.governance_store.delete_exec_approvals_for_context(task_id) or 0)
        except Exception:
            logger.warning('task wipe: governance cleanup failed for {}', task_id, exc_info=True)
            return 0

    def _discard_task_memory_caches(self, task_id: str) -> None:
        """S7：清掉以 task_id 为键的进程内缓存（各进程清自己的）。"""
        normalized = str(task_id or '').strip()
        if not normalized:
            return
        self._pending_task_summaries.pop(normalized, None)
        self._last_summary_payloads.pop(normalized, None)
        flush_task = self._task_summary_flush_tasks.pop(normalized, None)
        if flush_task is not None:
            try:
                flush_task.cancel()
            except Exception:
                pass
        try:
            with self._terminal_cleanup_inflight_guard:
                self._terminal_cleanup_inflight.discard(normalized)
        except Exception:
            pass
        try:
            self.log_service.discard_task_caches(normalized)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 产出导出：删除/purge 前把终态保留清单命中的 artifact 复制到
    # .g3ku/main-runtime/deliverables/<safe_task_id>/（永久保留，不参与磁盘治理）。
    # ------------------------------------------------------------------

    def _export_task_deliverables(self, task: TaskRecord, *, reason: str) -> dict[str, Any]:
        """导出产出（best-effort）：任何失败只 warning，不阻断删除。

        - 判据复用 _terminal_artifact_keep_policy（kind∈{patch,final_output}、
          final_output_ref 指向、标题含 report/summary）；
        - 文本经 read_artifact_text 统一读端（gzip 自动解压）明文落盘；
          非 UTF-8 走原样字节（state=raw）；取不到内容记 state=missing；
        - 无 kept 或全部 missing 不留目录（避免空壳堆积）。
        """
        task_id = str(task.task_id or '').strip()
        result: dict[str, Any] = {'exported': 0, 'missing': 0, 'dir': ''}
        if not task_id:
            return result
        try:
            artifacts = self.list_artifacts(task_id)
        except Exception:
            artifacts = []
        kept = [item for item in artifacts or [] if self._terminal_artifact_keep_policy(task, item)]
        if not kept:
            return result
        if not has_emergency_disk_budget((self._deliverables_dir, self.store.path)):
            logger.warning('task wipe: deliverables export skipped (no disk budget) for {}', task_id)
            return result
        safe_name = task_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        target_dir = self._deliverables_dir / safe_name
        files_meta: list[dict[str, Any]] = []
        exported = 0
        missing = 0
        for artifact in kept:
            artifact_id = str(getattr(artifact, 'artifact_id', '') or '').strip()
            title = str(getattr(artifact, 'title', '') or '').strip() or artifact_id
            kind = str(getattr(artifact, 'kind', '') or '').strip()
            source_path = str(getattr(artifact, 'path', '') or '').strip()
            encoding = str(getattr(artifact, 'content_encoding', '') or '').strip() or 'plain'
            content_bytes = b''
            state = 'missing'
            exported_name = ''
            try:
                text = read_artifact_text(artifact)
            except Exception:
                text = ''
            if text:
                state = 'ok'
                content_bytes = text.encode('utf-8')
            else:
                raw = self._deliverable_raw_bytes(source_path)
                if raw is not None:
                    state = 'raw'
                    content_bytes = raw
            if state != 'missing':
                exported_name = self._deliverable_file_name(
                    title=title, artifact_id=artifact_id, source_path=source_path, binary=(state == 'raw'),
                )
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    # 覆盖式导出：补偿重放幂等。
                    (target_dir / exported_name).write_bytes(content_bytes)
                    exported += 1
                except OSError:
                    state = 'missing'
                    exported_name = ''
                    content_bytes = b''
            if state == 'missing':
                missing += 1
            files_meta.append({
                'artifact_id': artifact_id,
                'kind': kind,
                'title': title,
                'exported_name': exported_name,
                'source_path': source_path,
                'source_encoding': encoding,
                'bytes': len(content_bytes),
                'state': state,
            })
        if not exported:
            if target_dir.exists():
                remove_tree(target_dir)
            result['missing'] = missing
            if missing:
                logger.warning('task wipe: all {} deliverables missing for {}', missing, task_id)
            return result
        manifest = {
            'schema_version': 1,
            'task_id': task_id,
            'session_id': str(task.session_id or ''),
            'title': str(task.title or ''),
            'status': str(task.status or ''),
            'reason': str(reason or ''),
            'exported_at': now_iso(),
            'files': files_meta,
            'counts': {'kept': len(kept), 'exported': exported, 'missing': missing},
        }
        try:
            (target_dir / 'manifest.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8',
            )
        except OSError:
            pass
        result.update({'exported': exported, 'missing': missing, 'dir': str(target_dir)})
        return result

    @staticmethod
    def _deliverable_raw_bytes(source_path: str) -> bytes | None:
        """非 UTF-8 产出：按源文件原样字节导出（gzip 存储的自动解压）。"""
        raw_path = str(source_path or '').strip()
        if not raw_path:
            return None
        try:
            path = Path(raw_path)
            if not path.is_file():
                return None
            data = path.read_bytes()
            if data[:2] == b'\x1f\x8b':
                import gzip as _gzip
                data = _gzip.decompress(data)
            return data
        except Exception:
            return None

    @staticmethod
    def _deliverable_file_name(*, title: str, artifact_id: str, source_path: str, binary: bool = False) -> str:
        """导出文件名：<标题 slug(≤60)>__<artifact_id 末 8 位><ext>（id 后缀天然去重）。"""
        suffix = Path(source_path).suffix.lower() if source_path else ''
        if suffix == '.gz':
            suffix = Path(Path(source_path).stem).suffix.lower()
        if not suffix:
            suffix = '.bin' if binary else '.md'
        slug_source = str(title or '').strip() or str(artifact_id or 'deliverable')
        slug = ''.join(ch if (ch.isalnum() or ch in '-_.') else '_' for ch in slug_source)[:60].strip('_.') or 'deliverable'
        tail = ''.join(ch for ch in str(artifact_id or '') if ch.isalnum())[-8:] or '00000000'
        return f'{slug}__{tail}{suffix}'

    # ------------------------------------------------------------------
    # 紧急线水位判定（只做保护，不触发任何删除）：跌破紧急线后运行中任务
    # 自动暂停、工具调用排队、可降级写跳过；任务数据只随用户/模型工具手动
    # 删除而清除（_wipe_task_data），磁盘紧张时靠任务大小可视化引导手动清理。
    # ------------------------------------------------------------------

    def _disk_waterline(self) -> tuple[int, int] | None:
        return disk_waterline_snapshot([str(data_root())])

    def _disk_emergency_due(self) -> bool:
        snapshot = self._disk_waterline()
        if snapshot is None:
            return False
        free, total = snapshot
        return free < emergency_threshold_bytes(total)

    # ------------------------------------------------------------------
    # 删除台账 sweep：遗留墓碑清扫 → wiped=0 补偿 → 台账过期 → 孤儿目录清扫。
    # ------------------------------------------------------------------

    async def _run_delete_ledger_sweep_if_due(self) -> None:
        """小时级维护循环挂载点（跨进程卡权 + 紧急态跳过，模式对齐明细裁剪）。"""
        if self._disk_emergency_due():
            return  # 补偿删除也是 IO/写放大源，紧急态不跑
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._LEDGER_RETENTION_DAYS)).isoformat(timespec='seconds')
        try:
            claimed = await asyncio.to_thread(
                self.store.claim_maintenance_run,
                'delete_ledger_sweep',
                min_interval_seconds=23 * 3600.0,
                detail=f'cutoff={cutoff}',
            )
        except Exception:
            return
        if not claimed:
            return
        tombstones = 0
        compensated = 0
        expired = 0
        # 1) 遗留 purged 墓碑（旧删除渐进产物）：zip 已不在、无产出可导，直接全删。
        try:
            tasks = await asyncio.to_thread(self.store.list_tasks)
        except Exception:
            tasks = []
        legacy = [
            task for task in tasks or []
            if str((dict(getattr(task, 'metadata', None) or {}).get('purged_at') or '')).strip()
        ][:50]
        for task in legacy:
            try:
                await self._wipe_task_data(task, reason='legacy_tombstone_sweep')
                tombstones += 1
            except Exception:
                continue
        # 2) wiped=0 补偿：台账写入后中断的删除，幂等重放文件/DB/governance 清除。
        try:
            pending_rows = await asyncio.to_thread(self.store.list_task_delete_ledger_rows, wiped=0, limit=200)
        except Exception:
            pending_rows = []
        for row in pending_rows:
            try:
                if await asyncio.to_thread(self._compensate_wipe, str(row.get('task_id') or '')):
                    compensated += 1
            except Exception:
                continue
        # 3) 台账过期：撤守卫前做最终补偿，再删台账行。
        try:
            expired_rows = await asyncio.to_thread(self.store.list_task_delete_ledger_rows, older_than_iso=cutoff, limit=200)
        except Exception:
            expired_rows = []
        for row in expired_rows:
            task_id = str(row.get('task_id') or '')
            if not task_id:
                continue
            try:
                await asyncio.to_thread(self._compensate_wipe, task_id)
                await asyncio.to_thread(self.store.delete_task_delete_ledger_row, task_id)
                expired += 1
            except Exception:
                continue
        # 4) 孤儿 event-history 目录清扫（删除链路失败残留；7 天宽限保护新任务）。
        orphan_dirs = 0
        try:
            orphan_dirs = int(await asyncio.to_thread(
                self.store._sweep_orphan_event_history_dirs, grace_seconds=7 * 24 * 3600.0,
            ) or 0)
        except Exception:
            orphan_dirs = 0
        if tombstones or compensated or expired or orphan_dirs:
            logger.info(
                'disk governance: delete ledger sweep tombstones={} compensated={} expired={} orphan_dirs={}',
                tombstones,
                compensated,
                expired,
                orphan_dirs,
            )

    async def _run_console_log_cap_if_due(self) -> None:
        """web 自身 stdout 落点的长跑封顶（跨进程卡权，模式对齐明细裁剪）。

        只就地截尾、不改名：文件是 bootstrap 交给 web 子进程的 append 句柄，
        句柄在就换不掉（Windows 上被占用的文件 rename 失败）。整份改名轮转发生在
        下一次启动（``negi_bootstrap._rotate_runtime_console_log``）。
        """
        try:
            claimed = await asyncio.to_thread(
                self.store.claim_maintenance_run,
                'console_log_cap',
                min_interval_seconds=_CONSOLE_LOG_CAP_MIN_INTERVAL_SECONDS,
            )
        except Exception:
            return
        if not claimed:
            return
        try:
            dropped = await asyncio.to_thread(
                self._cap_console_log_file,
                WEB_LOG_FILE,
                cap_bytes=_CONSOLE_LOG_CAP_BYTES,
                keep_bytes=_CONSOLE_LOG_KEEP_BYTES,
            )
        except Exception:
            return
        if dropped:
            logger.info('disk governance: console.log capped, dropped {} bytes', dropped)

    @staticmethod
    def _cap_console_log_file(log_path: Path, *, cap_bytes: int, keep_bytes: int) -> int:
        """超过 cap 时只留尾部 keep_bytes（对齐到整行），返回丢掉的字节数；未超返回 0。"""
        try:
            size = int(log_path.stat().st_size)
        except OSError:
            return 0
        if size <= cap_bytes:
            return 0
        with log_path.open('rb') as handle:
            handle.seek(max(0, size - keep_bytes))
            handle.readline()  # 丢掉 seek 落在半行处的那半行
            tail = handle.read()
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        marker = f'\n[log-cap] {stamp} 此前 {size - len(tail)} 字节已丢弃，仅保留尾部\n'.encode('utf-8')
        with log_path.open('r+b') as handle:
            handle.truncate(0)
            handle.write(marker + tail)
            handle.flush()
        return size - len(tail)

    def _compensate_wipe(self, task_id: str) -> bool:
        """仅凭 task_id 幂等重放 wipe 的文件/DB/governance 子集（不导出、不发 task.deleted）。"""
        normalized = str(task_id or '').strip()
        if not normalized:
            return False
        task = self.get_task(normalized)
        if task is not None:
            temp_paths = list(dict.fromkeys((
                self._effective_task_temp_dir(normalized),
                self._task_temp_dir(normalized, create=False),
            )))
        else:
            # runtime_meta 已删：回退确定性路径。
            temp_paths = [self._task_temp_dir(normalized, create=False)]
        self._wipe_task_files(normalized, temp_paths)
        try:
            self.store.delete_task(normalized)
        except Exception:
            return False
        try:
            self.store.mark_shutdown_pause_entry_consumed(kind='task', ref_id=normalized)
        except Exception:
            pass
        self._wipe_governance_rows(normalized)
        try:
            self.store.mark_task_delete_wiped(normalized)
        except Exception:
            pass
        return True

    def _resource_base_dir(self, kind: ResourceKind) -> Path:
        manager = getattr(self, '_resource_manager', None)
        registry = getattr(manager, '_registry', None)
        if kind is ResourceKind.SKILL:
            candidate = getattr(registry, 'skills_dir', None)
            fallback = self._workspace_root() / 'skills'
        else:
            candidate = getattr(registry, 'tools_dir', None)
            fallback = self._workspace_root() / 'tools'
        return Path(candidate or fallback).resolve(strict=False)

    @staticmethod
    def _is_relative_to(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    def _resolve_workspace_path(self, raw_path: str | Path | None) -> Path:
        path = Path(raw_path or '').expanduser()
        if not path.is_absolute():
            path = self._workspace_root() / path
        return path.resolve(strict=False)

    def _resolve_resource_root(self, raw_path: str | Path | None, *, kind: ResourceKind) -> Path:
        resolved = self._resolve_workspace_path(raw_path)
        base_dir = self._resource_base_dir(kind)
        if not self._is_relative_to(resolved, base_dir):
            raise ValueError(f'{kind.value}_path_outside_workspace')
        if resolved == base_dir:
            raise ValueError(f'{kind.value}_path_invalid')
        return resolved

    def _resource_is_busy(self, kind: ResourceKind, *names: str) -> bool:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'busy_state'):
            return False
        for raw_name in names:
            name = str(raw_name or '').strip()
            if not name:
                continue
            try:
                state = manager.busy_state(kind, name)
            except Exception:
                continue
            if bool(getattr(state, 'busy', False)):
                return True
        return False

    @staticmethod
    def _display_role_label(role: str) -> str:
        return {
            'ceo': '主Agent',
            'execution': '执行',
            'inspection': '检验',
        }.get(str(role or '').strip().lower(), str(role or '').strip())

    def _running_task_records(self) -> list[TaskRecord]:
        try:
            tasks = self.store.list_tasks()
        except Exception:
            return []
        return [
            task
            for task in tasks
            if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress' and not bool(getattr(task, 'is_paused', False))
        ]

    def _running_ceo_session_ids(self) -> list[str]:
        loop = getattr(self, '_runtime_loop', None)
        active_tasks = getattr(loop, '_active_tasks', None)
        if not isinstance(active_tasks, dict):
            return []
        session_ids: set[str] = set()
        for raw_session_id, bucket in active_tasks.items():
            session_id = str(raw_session_id or '').strip()
            if not session_id.startswith('web:'):
                continue
            if not bucket:
                continue
            session_ids.add(session_id)
        return sorted(session_ids)

    def _session_title(self, session_id: str) -> str:
        loop = getattr(self, '_runtime_loop', None)
        session_manager = getattr(loop, 'sessions', None)
        if session_manager is None:
            return session_id
        get_path = getattr(session_manager, 'get_path', None)
        if callable(get_path):
            try:
                path = get_path(session_id)
                if path is not None and not Path(path).exists():
                    return session_id
            except Exception:
                return session_id
        get_or_create = getattr(session_manager, 'get_or_create', None)
        if not callable(get_or_create):
            return session_id
        try:
            session = get_or_create(session_id)
        except Exception:
            return session_id
        metadata = getattr(session, 'metadata', None) or {}
        title = str(metadata.get('title') or '').strip() if isinstance(metadata, dict) else ''
        return title or str(getattr(session, 'key', '') or '').strip() or session_id

    def _skill_visible_roles_for_task(self, task: TaskRecord, skill_id: str) -> list[str]:
        roles: list[str] = []
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        for actor_role in ('execution', 'inspection'):
            visible_ids = {
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)
            }
            if skill_id in visible_ids:
                roles.append(actor_role)
        return roles

    def _tool_visible_roles_for_task(self, task: TaskRecord, tool_id: str) -> list[str]:
        roles: list[str] = []
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        for actor_role in ('execution', 'inspection'):
            visible_ids = {
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in self.list_visible_tools(actor_role=actor_role, session_id=session_id)
            }
            if tool_id in visible_ids:
                roles.append(actor_role)
        return roles

    @classmethod
    def _format_usage_message(
        cls,
        *,
        resource_label: str,
        display_name: str,
        usage: dict[str, list[dict[str, Any]]],
    ) -> str:
        tasks = list(usage.get('tasks') or [])
        ceo_sessions = list(usage.get('ceo_sessions') or [])
        blockers: list[str] = []
        if tasks:
            blockers.append(f'{len(tasks)} 个进行中的任务')
        if ceo_sessions:
            blockers.append(f'{len(ceo_sessions)} 个正在运行的主Agent会话')
        message = f'无法删除{resource_label}“{display_name}”，当前有{"、".join(blockers)}正在使用。'
        previews: list[str] = []
        if tasks:
            task_text = '；'.join(
                (
                    f"{str(item.get('title') or item.get('task_id') or '未命名任务').strip()} ({str(item.get('task_id') or '').strip()})"
                    + (
                        f" / {'、'.join(cls._display_role_label(role) for role in list(item.get('actor_roles') or []))}"
                        if list(item.get('actor_roles') or [])
                        else ''
                    )
                )
                for item in tasks[:3]
            )
            if len(tasks) > 3:
                task_text += f'；等 {len(tasks)} 个'
            previews.append(f'任务：{task_text}')
        if ceo_sessions:
            ceo_text = '；'.join(
                f"{str(item.get('title') or item.get('session_id') or '未命名会话').strip()} ({str(item.get('session_id') or '').strip()})"
                for item in ceo_sessions[:3]
            )
            if len(ceo_sessions) > 3:
                ceo_text += f'；等 {len(ceo_sessions)} 个'
            previews.append(f'主Agent：{ceo_text}')
        return f"{message} {' '.join(previews)}".strip()

    def _skill_usage_summary(self, skill_id: str) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {'tasks': [], 'ceo_sessions': []}
        for task in self._running_task_records():
            actor_roles = self._skill_visible_roles_for_task(task, skill_id)
            if not actor_roles:
                continue
            usage['tasks'].append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'title': str(getattr(task, 'title', '') or '').strip(),
                    'session_id': str(getattr(task, 'session_id', '') or '').strip(),
                    'actor_roles': actor_roles,
                }
            )
        for session_id in self._running_ceo_session_ids():
            visible_ids = {
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in self.list_visible_skill_resources(actor_role='ceo', session_id=session_id)
            }
            if skill_id not in visible_ids:
                continue
            usage['ceo_sessions'].append(
                {
                    'session_id': session_id,
                    'title': self._session_title(session_id),
                }
            )
        return usage

    def _tool_usage_summary(self, tool_id: str) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {'tasks': [], 'ceo_sessions': []}
        for task in self._running_task_records():
            actor_roles = self._tool_visible_roles_for_task(task, tool_id)
            if not actor_roles:
                continue
            usage['tasks'].append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'title': str(getattr(task, 'title', '') or '').strip(),
                    'session_id': str(getattr(task, 'session_id', '') or '').strip(),
                    'actor_roles': actor_roles,
                }
            )
        for session_id in self._running_ceo_session_ids():
            visible_ids = {
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in self.list_visible_tools(actor_role='ceo', session_id=session_id)
            }
            if tool_id not in visible_ids:
                continue
            usage['ceo_sessions'].append(
                {
                    'session_id': session_id,
                    'title': self._session_title(session_id),
                }
            )
        return usage

    def _raise_if_skill_in_use(self, skill) -> None:
        target_skill_id = str(getattr(skill, 'skill_id', '') or '').strip()
        display_name = str(getattr(skill, 'display_name', '') or target_skill_id).strip() or target_skill_id
        usage = self._skill_usage_summary(target_skill_id)
        if not usage['tasks'] and not usage['ceo_sessions']:
            return
        raise ResourceDeleteBlockedError(
            code='skill_in_use',
            message=self._format_usage_message(resource_label='Skill', display_name=display_name, usage=usage),
            resource_kind='skill',
            resource_id=target_skill_id,
            usage=usage,
        )

    def _raise_if_tool_in_use(self, record) -> None:
        target_tool_id = str(getattr(record, 'tool_id', '') or '').strip()
        display_name = str(getattr(record, 'display_name', '') or target_tool_id).strip() or target_tool_id
        usage = self._tool_usage_summary(target_tool_id)
        if not usage['tasks'] and not usage['ceo_sessions']:
            return
        raise ResourceDeleteBlockedError(
            code='tool_in_use',
            message=self._format_usage_message(resource_label='工具', display_name=display_name, usage=usage),
            resource_kind='tool',
            resource_id=target_tool_id,
            usage=usage,
        )

    def _delete_path(self, path: Path, *, deleted_paths: list[str]) -> None:
        if not path.exists():
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            return
        except Exception as exc:
            raise ValueError(f'resource_delete_failed:{path}:{exc}') from exc
        deleted_paths.append(str(path))

    def _collect_workspace_delete_path(
        self,
        raw_path: str | Path | None,
        *,
        delete_paths: set[Path],
        skipped_paths: list[str],
    ) -> None:
        text = str(raw_path or '').strip()
        if not text:
            return
        resolved = self._resolve_workspace_path(text)
        workspace_root = self._workspace_root()
        if not self._is_relative_to(resolved, workspace_root):
            skipped_paths.append(str(resolved))
            return
        if resolved == workspace_root:
            skipped_paths.append(str(resolved))
            return
        delete_paths.add(resolved)

    def _remember_retired_tool_schema(self, tool_id: str) -> None:
        """删除前把工具的 provider 参数表抄进快照，供渲染侧在没有实例时继续发出同一份。

        抄的是 `to_model_schema()`——两条车道各自再投影一次（前门剥 schema 组合子、
        节点原样用），所以快照存的必须是"实例会给出的那一份"，不能是已投影过的形状。
        抄不到就放弃：宁可这一跳断一次前缀，也不要发一份与上一跳不同的假 schema。
        """
        manager = getattr(self, '_resource_manager', None)
        instance_getter = getattr(manager, 'tool_instances', None)
        instances = dict(instance_getter() or {}) if callable(instance_getter) else {}
        tool = instances.get(str(tool_id or '').strip())
        to_schema = getattr(tool, 'to_model_schema', None)
        if not callable(to_schema):
            return
        try:
            schema = to_schema()
        except Exception:
            return
        if isinstance(schema, dict):
            record_tool_schema(tool_id, schema)

    def _delete_ordered_paths(self, paths: list[Path]) -> list[str]:
        """按给定顺序删除路径。

        这是整条删除流程里唯一可能慢的动作：MCP 工具的安装目录常带 node_modules，
        Windows 上 rmtree 十几到几十秒都正常。异步车道把它交给线程，别让一次删除
        把整个 web 事件循环（含 websocket 与其它管理请求）一起冻住。
        """
        deleted_paths: list[str] = []
        for path in list(paths or []):
            self._delete_path(path, deleted_paths=deleted_paths)
        return deleted_paths

    def _prepare_skill_resource_delete(self, skill_id: str) -> dict[str, Any]:
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            raise ValueError('skill_not_found')
        target_skill_id = str(skill.skill_id or '').strip()
        self._raise_if_skill_in_use(skill)
        if self._resource_is_busy(ResourceKind.SKILL, target_skill_id):
            raise ValueError('skill_busy')
        skill_root = self._resolve_resource_root(skill.source_path, kind=ResourceKind.SKILL)
        return {
            'target_id': target_skill_id,
            'root': skill_root,
            'paths': [skill_root],
            # 基线必须在删除前取：删完再扫就只剩"已删除"这一种状态。
            'before_state': self.capture_resource_tree_state(),
        }

    def _finish_skill_resource_delete(
        self,
        prepared: dict[str, Any],
        deleted_paths: list[str],
        *,
        session_id: str,
    ) -> dict[str, Any]:
        target_skill_id = str(prepared.get('target_id') or '').strip()
        refresh_result = self.refresh_changed_resources(
            prepared.get('before_state'),
            trigger='skill-delete',
            session_id=session_id,
        )
        self.governance_store.delete_role_policies_for_resource(
            resource_kind='skill',
            resource_id=target_skill_id,
        )
        return {
            'skill_id': target_skill_id,
            'path': str(prepared.get('root') or ''),
            'deleted_paths': list(deleted_paths or []),
            'resources': refresh_result,
        }

    def delete_skill_resource(self, skill_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        prepared = self._prepare_skill_resource_delete(skill_id)
        deleted_paths = self._delete_ordered_paths(list(prepared.get('paths') or []))
        return self._finish_skill_resource_delete(prepared, deleted_paths, session_id=session_id)

    async def delete_skill_resource_async(
        self,
        skill_id: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        target_skill_id = str(skill_id or '').strip()
        prepared = self._prepare_skill_resource_delete(target_skill_id)
        deleted_paths = await asyncio.to_thread(
            self._delete_ordered_paths,
            list(prepared.get('paths') or []),
        )
        item = self._finish_skill_resource_delete(prepared, deleted_paths, session_id=session_id)
        item.update(await self._sync_catalog_targets(skill_ids={target_skill_id}))
        return item

    def update_skill_policy(self, skill_id: str, *, session_id: str = 'web:shared', enabled: bool | None = None, allowed_roles: list[str] | None = None):
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            return None
        updated = skill.model_copy(update={
            'enabled': skill.enabled if enabled is None else bool(enabled),
            'allowed_roles': list(skill.allowed_roles if allowed_roles is None else allowed_roles),
        })
        self.governance_store.upsert_skill_resource(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return updated

    def enable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return self.update_skill_policy(skill_id, session_id=session_id, enabled=True)

    def disable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return self.update_skill_policy(skill_id, session_id=session_id, enabled=False)

    def _raw_tool(self, tool_id: str):
        return self.resource_registry.get_tool(str(tool_id or '').strip())

    def _configured_core_tool_entries(self) -> list[str]:
        if self._resource_manager is not None:
            descriptor = self._resource_manager.get_tool_descriptor('memory_runtime')
            if descriptor is not None:
                try:
                    settings = validate_tool_settings(
                        MemoryRuntimeSettings,
                        raw_tool_settings_from_descriptor(descriptor),
                        tool_name='memory_runtime',
                    )
                    return [str(item).strip() for item in list(settings.assembly.core_tools or []) if str(item).strip()]
                except Exception:
                    pass
        return configured_core_tools(resource_manager=self._resource_manager)

    def _core_tool_resolution(self):
        return resolve_core_tool_targets(
            self._configured_core_tool_entries(),
            list(self.resource_registry.list_tools()),
        )

    def _decorate_tool(self, record):
        if record is None:
            return None
        resolution = self._core_tool_resolution()
        metadata = dict(getattr(record, 'metadata', {}) or {})
        metadata['repair_required'] = bool(getattr(record, 'callable', True)) and not bool(getattr(record, 'available', True))
        normalized_tool_id = str(getattr(record, 'tool_id', '') or '').strip()
        return record.model_copy(
            update={
                'is_core': normalized_tool_id in resolution.tool_ids,
                'metadata': metadata,
            }
        )

    def list_tool_resources(self) -> list[Any]:
        return [self._decorate_tool(item) for item in self.resource_registry.list_tools()]

    def get_tool(self, tool_id: str):
        return self._decorate_tool(self._raw_tool(tool_id))

    def _ceo_review_candidate_tools(self) -> list[Any]:
        """默认免审档的派生样本：CEO 角色在默认会话下可见的那批工具记录。"""
        return list(self.list_visible_tools(actor_role='ceo', session_id='web:shared') or [])

    def get_governance_mode(self) -> dict[str, Any]:
        enabled = self.governance_store.get_bool_meta(GOVERNANCE_MODE_META_KEY, default=False)
        review_free, customized = resolve_review_free_tool_ids(
            self.governance_store,
            self._ceo_review_candidate_tools(),
        )
        return {
            'enabled': bool(enabled),
            'mode': review_mode_from_enabled(enabled),
            'whitelist_tools': review_free,
            'whitelist_customized': bool(customized),
            'updated_at': str(self.governance_store.get_meta(GOVERNANCE_MODE_UPDATED_AT_META_KEY) or ''),
        }

    def update_governance_mode(
        self,
        *,
        enabled: bool | None = None,
        whitelist_tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        if enabled is not None:
            self.governance_store.set_bool_meta(GOVERNANCE_MODE_META_KEY, bool(enabled))
        if whitelist_tools is not None:
            write_review_free_tool_ids(self.governance_store, whitelist_tools)
        self.governance_store.set_meta(GOVERNANCE_MODE_UPDATED_AT_META_KEY, now_iso())
        return self.get_governance_mode()

    @staticmethod
    def _normalize_permission_risk_level(value: Any, *, default: str = 'medium') -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'low', 'medium', 'high'}:
            return normalized
        fallback = str(default or 'medium').strip().lower()
        return fallback if fallback in {'low', 'medium', 'high'} else 'medium'

    def frontdoor_reviewable_tool_risk_map(self, *, actor_role: str, session_id: str) -> dict[str, str]:
        """条件放行档下需要人工审批的工具 → 风险度。

        免审集合由名单决定：操作员存过名单就照名单，没存过则默认放行低风险工具。
        """
        if not bool(self.governance_store.get_bool_meta(GOVERNANCE_MODE_META_KEY, default=False)):
            return {}
        normalized_actor_role = str(actor_role or 'ceo').strip().lower() or 'ceo'
        normalized_session_id = self._normalize_session_key(session_id)
        visible_names = set(
            self.list_effective_tool_names(
                actor_role=normalized_actor_role,
                session_id=normalized_session_id,
            )
        )
        if not visible_names:
            return {}
        candidates = []
        for record in list(
            self.list_visible_tools(actor_role=normalized_actor_role, session_id=normalized_session_id) or []
        ):
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            if not tool_id or tool_id not in visible_names:
                continue
            candidates.append(record)
        review_free, _ = resolve_review_free_tool_ids(self.governance_store, candidates)
        review_free_ids = set(review_free)
        reviewable: dict[str, str] = {}
        for record in candidates:
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            if tool_id in review_free_ids:
                continue
            reviewable[tool_id] = self._normalize_permission_risk_level(getattr(record, 'risk_level', 'medium'))
        return reviewable

    def get_tool_toolskill(self, tool_id: str) -> dict[str, Any] | None:
        return build_tool_toolskill_payload(
            str(tool_id or '').strip(),
            raw_tool_getter=self._raw_tool,
            resource_registry=self.resource_registry,
            resource_manager=self._resource_manager,
        )

    def resolve_node_seed_contracts(
        self,
        *,
        task,
        node_kind: str,
        declared: list[str] | None,
    ) -> dict[str, Any]:
        """派发方声明的工具名 → 预置契约消息 + 回执。纯读，不落任何跨跳状态。

        渲染链与 loader / 冲动路径**同一个**（`get_tool_toolskill` →
        `build_tool_context_fingerprint` → `render_tool_contract`）：自己拼块或自算指纹都会让
        字节对不上，而症状只是「节点仍然走冲动路径」，查不出来。

        闸门按**目标节点的角色**实时可见集判，与名单来源无关。声明过的名字必须出现在
        `accepted` 或 `dropped` 之一——没有第三条静默丢弃的路。
        """
        names = normalize_seed_tool_names(declared)
        if not names:
            return {'seed_tools': [], 'messages': [], 'accepted': [], 'dropped': []}
        actor_role = 'inspection' if str(node_kind or '').strip().lower() == 'acceptance' else 'execution'
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        visible_names = {
            str(name or '').strip()
            for name in list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
            if str(name or '').strip()
        }
        candidates: list[dict[str, Any]] = []
        dropped: list[dict[str, str]] = []
        for name in names:
            payload = self.get_tool_toolskill(name)
            record_exists = self._raw_tool(name) is not None
            has_payload = isinstance(payload, dict)
            fingerprint = ''
            block = ''
            if has_payload:
                fingerprint = build_tool_context_fingerprint(payload)
                if fingerprint:
                    block = render_tool_contract({**payload, 'tool_context_fingerprint': fingerprint})
            reason = seed_drop_reason(
                is_never_contract=name in NODE_NEVER_CONTRACT_TOOL_NAMES,
                is_known=record_exists,
                is_visible=name in visible_names,
                is_repair_required=bool(payload.get('repair_required')) if has_payload else False,
                has_toolskill_body=bool(str(payload.get('content') or '').strip()) if has_payload else False,
                rendered_block=block,
            )
            if reason:
                dropped.append({'tool': name, 'reason': reason})
                continue
            candidates.append({'tool': name, 'rev': fingerprint, 'chars': len(block), 'content': block})
        kept, size_dropped = trim_seed_contracts(candidates)
        dropped.extend(size_dropped)
        return {
            'seed_tools': [str(item.get('tool') or '') for item in kept],
            'messages': [{'role': 'user', 'content': str(item.get('content') or '')} for item in kept],
            'accepted': [
                {
                    'tool': str(item.get('tool') or ''),
                    'rev': str(item.get('rev') or ''),
                    'chars': int(item.get('chars') or 0),
                }
                for item in kept
            ],
            'dropped': dropped,
        }

    def build_node_seed_metadata(self, *, task, node_kind: str, declared: list[str] | None) -> dict[str, Any]:
        """派发时要落进节点 metadata 的那一份预置判据；未声明时返回空 dict。

        解析失败**不阻断派生**（预置是优化不是前置条件）：此时名单照原样留下、回执整份缺席，
        只记 `seed_error`——给读不出来的名字编一条 reason 就是把「我不知道」解析成否定判决。
        首跳构建时会重新解析一次并把结果写进 `seeded_*` 观测，那里才是节点真正拿到的东西。
        """
        names = normalize_seed_tool_names(declared)
        if not names:
            return {}
        try:
            resolved = self.resolve_node_seed_contracts(task=task, node_kind=node_kind, declared=names)
        except Exception as exc:
            return {'seed_tools': names, 'seed_error': describe_exception(exc)}
        accepted = list(resolved.get('accepted') or [])
        dropped = list(resolved.get('dropped') or [])
        return {
            'seed_tools': list(resolved.get('seed_tools') or []),
            'seed_receipt': {'accepted': accepted, 'dropped': dropped},
            # 观测从派发这一刻就有初值：节点被建出来却没被驱动过时，「没声明」「声明了全被拒」
            # 与「预置成功了」在库里必须长得不一样。
            'seeded_contracts': accepted,
            'seeded_dropped': dropped,
        }

    def _prepare_tool_resource_delete(self, tool_id: str) -> dict[str, Any]:
        record = self._raw_tool(tool_id)
        if record is None:
            raise ValueError('tool_not_found')

        target_tool_id = str(record.tool_id or '').strip()
        if target_tool_id in self._core_tool_resolution().tool_ids:
            raise ResourceMutationBlockedError(
                code='core_tool_delete_forbidden',
                message='Core tools cannot be deleted.',
                resource_kind='tool',
                resource_id=target_tool_id,
            )
        self._raise_if_tool_in_use(record)

        descriptor = (
            self._resource_manager.get_tool_descriptor(target_tool_id)
            if self._resource_manager is not None and hasattr(self._resource_manager, 'get_tool_descriptor')
            else None
        )
        if self._resource_is_busy(ResourceKind.TOOL, target_tool_id):
            raise ValueError('tool_busy')

        # 删除前先抄一份 provider 参数表：实例消失后 `tools[]` 仍要按原字节发出，
        # 否则这一跳的工具表少一条，可复用前缀整段重付（与权限收回的缓存待遇对齐）。
        self._remember_retired_tool_schema(target_tool_id)

        before_state = self.capture_resource_tree_state()
        delete_paths: set[Path] = set()
        skipped_paths: list[str] = []
        delete_paths.add(self._resolve_resource_root(record.source_path, kind=ResourceKind.TOOL))
        if descriptor is not None:
            delete_paths.add(self._resolve_resource_root(descriptor.root, kind=ResourceKind.TOOL))
            self._collect_workspace_delete_path(
                getattr(descriptor, 'install_dir', None),
                delete_paths=delete_paths,
                skipped_paths=skipped_paths,
            )
        self._collect_workspace_delete_path(
            getattr(record, 'install_dir', None),
            delete_paths=delete_paths,
            skipped_paths=skipped_paths,
        )
        ordered_paths = sorted(delete_paths, key=lambda item: (len(str(item)), str(item)), reverse=True)
        return {
            'target_id': target_tool_id,
            'root': self._resolve_resource_root(record.source_path, kind=ResourceKind.TOOL),
            'paths': ordered_paths,
            'skipped_paths': skipped_paths,
            'before_state': before_state,
        }

    def _finish_tool_resource_delete(
        self,
        prepared: dict[str, Any],
        deleted_paths: list[str],
        *,
        session_id: str,
    ) -> dict[str, Any]:
        target_tool_id = str(prepared.get('target_id') or '').strip()
        refresh_result = self.refresh_changed_resources(
            prepared.get('before_state'),
            trigger='tool-delete',
            session_id=session_id,
        )
        self.governance_store.delete_role_policies_for_resource(
            resource_kind='tool',
            resource_id=target_tool_id,
        )
        return {
            'tool_id': target_tool_id,
            'path': str(prepared.get('root') or ''),
            'deleted_paths': list(deleted_paths or []),
            'skipped_paths': list(prepared.get('skipped_paths') or []),
            'resources': refresh_result,
        }

    def delete_tool_resource(self, tool_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        prepared = self._prepare_tool_resource_delete(tool_id)
        deleted_paths = self._delete_ordered_paths(list(prepared.get('paths') or []))
        return self._finish_tool_resource_delete(prepared, deleted_paths, session_id=session_id)

    async def delete_tool_resource_async(
        self,
        tool_id: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        target_tool_id = str(tool_id or '').strip()
        prepared = self._prepare_tool_resource_delete(target_tool_id)
        # 目录删除走线程：一次 MCP 工具删除动辄十几秒，压在事件循环上等于整站停摆。
        deleted_paths = await asyncio.to_thread(
            self._delete_ordered_paths,
            list(prepared.get('paths') or []),
        )
        item = self._finish_tool_resource_delete(prepared, deleted_paths, session_id=session_id)
        item.update(await self._sync_catalog_targets(tool_ids={target_tool_id}))
        return item

    def update_tool_policy(
        self,
        tool_id: str,
        *,
        session_id: str = 'web:shared',
        enabled: bool | None = None,
        allowed_roles: list[str] | None = None,
        execution_mode: str | None = None,
    ):
        record = self._raw_tool(tool_id)
        if record is None:
            return None
        target_tool_id = str(getattr(record, 'tool_id', '') or '').strip()
        if allowed_roles is not None and str(getattr(record, 'admin_mode', 'editable') or 'editable') == 'readonly_system':
            normalized_roles = normalize_public_allowed_roles([str(role) for role in list(allowed_roles or [])])
            current_roles = normalize_public_allowed_roles(list(getattr(record, 'allowed_roles', []) or []))
            if normalized_roles != current_roles:
                raise ResourceMutationBlockedError(
                    code='tool_readonly',
                    message='Readonly system tools cannot be edited.',
                    resource_kind='tool',
                    resource_id=target_tool_id,
                )
        if execution_mode is not None and not exec_tool_supports_execution_mode(target_tool_id):
            raise ResourceMutationBlockedError(
                code='tool_execution_mode_unsupported',
                message='execution_mode is only supported for exec.',
                resource_kind='tool',
                resource_id=target_tool_id,
            )
        next_roles = (
            list(getattr(record, 'allowed_roles', []) or [])
            if allowed_roles is None
            else normalize_public_allowed_roles([str(role) for role in list(allowed_roles or [])])
        )
        updated = record.model_copy(
            update={
                'enabled': record.enabled if enabled is None else bool(enabled),
                'allowed_roles': next_roles,
                'metadata': merge_exec_execution_mode_metadata(
                    getattr(record, 'metadata', {}) or {},
                    execution_mode=execution_mode,
                ),
            }
        )
        self.governance_store.upsert_tool(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return self.get_tool(target_tool_id)

    def enable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return self.update_tool_policy(tool_id, session_id=session_id, enabled=True)

    def disable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return self.update_tool_policy(tool_id, session_id=session_id, enabled=False)

    def is_tool_allowed(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> bool:
        decision = self.policy_engine.evaluate_tool(
            subject=self._subject(actor_role=actor_role, session_id=session_id, task_id=task_id, node_id=node_id),
            tool_id=str(tool_id or '').strip(),
        )
        return bool(decision.allowed)

    def reload_resources(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        if self._resource_manager is not None:
            self._resource_manager.reload_now(trigger='manual')
        skills, tools = self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        self._record_resource_tree_state()
        return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}

    async def reload_resources_async(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        result = self.reload_resources(session_id=session_id)
        sync_result = await self._sync_catalog_targets(
            skill_ids={
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in list(self.list_skill_resources() or [])
                if str(getattr(item, 'skill_id', '') or '').strip()
            },
            tool_ids={
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in list(self.list_tool_resources() or [])
                if str(getattr(item, 'tool_id', '') or '').strip()
            },
        )
        if bool(sync_result.get('catalog_synced')):
            result['catalog'] = sync_result.get('catalog', {'created': 0, 'updated': 0, 'removed': 0})
        else:
            result['catalog'] = {'created': 0, 'updated': 0, 'removed': 0}
        return result

    async def get_context_traces(self, *, trace_kind: str, limit: int = 20) -> dict[str, Any]:
        manager = self.memory_manager
        if manager is None or not hasattr(manager, 'read_trace_file'):
            return {'ok': True, 'items': [], 'trace_kind': trace_kind, 'limit': max(1, int(limit))}
        items = await manager.read_trace_file(trace_kind=trace_kind, limit=max(1, int(limit)))
        return {'ok': True, 'items': items, 'trace_kind': trace_kind, 'limit': max(1, int(limit))}

    def get_task_error_log_payload(self, task_id: str) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return None
        items = self.log_service.list_task_error_logs(normalized_task_id)
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'items': [item.model_dump(mode='json') for item in items],
        }

    def get_task_node_error_log_payload(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return None
        normalized_node_id = str(node_id or '').strip()
        items = self.log_service.list_task_node_error_logs(normalized_task_id, normalized_node_id)
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'node_id': normalized_node_id,
            'items': [item.model_dump(mode='json') for item in items],
        }

    async def _tool_watchdog_snapshot_supplier(self, task_id: str) -> dict[str, Any] | None:
        # 快照内容只按 task_id 取值，同任务并发的每一拍要的是同一份东西：合流成一次构建，
        # 而不是每个等待者各起一条 to_thread。shield 掉单个等待者的取消，取消不能连带
        # 掐掉别人正在等的同一份构建。
        normalized = self.normalize_task_id(str(task_id or '').strip())
        flight = self._watchdog_snapshot_flights.get(normalized)
        if flight is None:
            flight = asyncio.create_task(self._build_tool_watchdog_snapshot(normalized))
            self._watchdog_snapshot_flights[normalized] = flight
            flight.add_done_callback(
                lambda done, key=normalized: self._release_watchdog_snapshot_flight(key, done)
            )
        return await asyncio.shield(flight)

    def _release_watchdog_snapshot_flight(self, task_id: str, done: asyncio.Task) -> None:
        if self._watchdog_snapshot_flights.get(task_id) is done:
            self._watchdog_snapshot_flights.pop(task_id, None)
        if not done.cancelled():
            done.exception()

    async def _build_tool_watchdog_snapshot(self, task_id: str) -> dict[str, Any] | None:
        # 工具看门狗每 poll_interval_seconds（默认 5s）取一次运行快照。整份任务详情里
        # recent_model_calls 是全量账本（实盘单任务 1545 行 / 1.43 MB，且随任务增长），
        # 而 summarize_runtime_snapshot 只读 task / root_node / frontier——建完就丢。
        # 快照是只读观测旁路，不放线程里就会占住事件循环。
        return await asyncio.to_thread(
            self.get_task_detail_payload,
            task_id,
            mark_read=False,
            model_call_limit=0,
        )

    def get_task_detail_payload(
        self,
        task_id: str,
        *,
        mark_read: bool = False,
        model_call_limit: int | None = None,
    ) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        payload = self.query_service.get_task_snapshot(
            task_id,
            mark_read=mark_read,
            model_call_limit=model_call_limit,
        )
        if payload is None:
            return None
        return payload

    def get_task_token_ledger_payload(
        self,
        task_id: str,
        *,
        model_call_limit: int = 300,
    ) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        return self.query_service.get_task_token_ledger(
            task_id,
            model_call_limit=model_call_limit,
        )

    def get_task_model_call_page_payload(
        self,
        task_id: str,
        *,
        page: int = 1,
        size: int = 100,
        anchor_seq: int | None = None,
    ) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        return self.query_service.get_task_model_call_page(
            task_id,
            page=page,
            size=size,
            anchor_seq=anchor_seq,
        )

    def get_task_tree_snapshot_payload(
        self,
        task_id: str,
        *,
        max_nodes: int | None = None,
        after_node_id: str = '',
    ) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        snapshot = self.query_service.get_tree_snapshot(
            normalized_task_id,
            max_nodes=max_nodes,
            after_node_id=after_node_id,
        )
        if snapshot is None:
            return None
        return {'ok': True, **snapshot.model_dump(mode='json')}

    def get_task_tree_subtree_payload(
        self,
        task_id: str,
        node_id: str,
        *,
        round_id: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        snapshot = self.query_service.get_tree_subtree(
            normalized_task_id,
            node_id,
            round_id=round_id,
        )
        if snapshot is None:
            return None
        return {'ok': True, **snapshot.model_dump(mode='json')}

    def get_node_detail_payload(self, task_id: str, node_id: str, detail_level: str = 'summary') -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(node_id)
        normalized_detail_level = self._normalize_node_detail_level(detail_level)
        detail = self.query_service.get_node_detail(normalized_task_id, normalized_node_id, detail_level=normalized_detail_level)
        if detail is None:
            return None
        item = self._repair_legacy_display_payload(detail.model_dump(mode='json'))
        runtime_node = self.store.get_node(normalized_node_id)
        runtime_metadata = dict((runtime_node.metadata or {}) if runtime_node is not None else {})
        latest_live_distribution_round_id = ''
        if runtime_node is not None:
            latest_live_round = self.node_runner._latest_incomplete_spawn_round(parent=runtime_node)
            if latest_live_round is not None:
                latest_live_distribution_round_id = str(latest_live_round[0] or '').strip()
            elif self.node_runner.node_is_in_live_distribution_tree(
                task_id=normalized_task_id,
                node_id=normalized_node_id,
            ):
                latest_live_distribution_round_id = str(runtime_metadata.get('spawn_owner_round_id') or '').strip()
        item['spawn_owner_parent_node_id'] = str(runtime_metadata.get('spawn_owner_parent_node_id') or '').strip()
        item['spawn_owner_round_id'] = str(runtime_metadata.get('spawn_owner_round_id') or '').strip()
        item['spawn_owner_entry_index'] = int(runtime_metadata.get('spawn_owner_entry_index') or 0)
        item['spawn_owner_kind'] = str(runtime_metadata.get('spawn_owner_kind') or '').strip()
        item['latest_live_distribution_round_id'] = latest_live_distribution_round_id
        latest_context = self._node_latest_context_refs(normalized_task_id, normalized_node_id)
        if latest_context is not None:
            if not str(item.get('actual_request_ref') or '').strip():
                item['actual_request_ref'] = str(
                    latest_context.get('actual_request_ref') or latest_context.get('ref') or ''
                ).strip()
            if not str(item.get('prompt_cache_key_hash') or '').strip():
                item['prompt_cache_key_hash'] = str(latest_context.get('prompt_cache_key_hash') or '').strip()
            if not str(item.get('actual_request_hash') or '').strip():
                item['actual_request_hash'] = str(latest_context.get('actual_request_hash') or '').strip()
            if not int(item.get('actual_request_message_count') or 0):
                item['actual_request_message_count'] = int(latest_context.get('actual_request_message_count') or 0)
            if not str(item.get('actual_tool_schema_hash') or '').strip():
                item['actual_tool_schema_hash'] = str(latest_context.get('actual_tool_schema_hash') or '').strip()
        if normalized_detail_level == 'summary':
            item.pop('execution_trace', None)
        else:
            item.pop('execution_trace_summary', None)
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'node_id': normalized_node_id,
            'item': item,
        }

    def _node_latest_context_refs(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        """最近一次真实请求的指针与哈希：只读 runtime frame / 节点 metadata，不解析 artifact 正文。"""
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(node_id)
        task = self.get_task(normalized_task_id)
        node = self.store.get_node(normalized_node_id)
        if task is None or node is None or str(node.task_id or '').strip() != normalized_task_id:
            return None
        frame = self.store.get_task_runtime_frame(normalized_task_id, normalized_node_id)
        actual_request_ref = ''
        messages_ref = ''
        ref = ''
        prompt_cache_key_hash = ''
        actual_request_hash = ''
        actual_request_message_count = 0
        actual_tool_schema_hash = ''
        observed_input_truth: dict[str, Any] = {}
        if frame is not None:
            frame_payload = dict(frame.payload or {})
            actual_request_ref = str(frame_payload.get('actual_request_ref') or '').strip()
            messages_ref = str(frame_payload.get('messages_ref') or '').strip()
            prompt_cache_key_hash = str(frame_payload.get('prompt_cache_key_hash') or '').strip()
            actual_request_hash = str(frame_payload.get('actual_request_hash') or '').strip()
            actual_request_message_count = int(frame_payload.get('actual_request_message_count') or 0)
            actual_tool_schema_hash = str(frame_payload.get('actual_tool_schema_hash') or '').strip()
            if isinstance(frame_payload.get('observed_input_truth'), dict):
                observed_input_truth = dict(frame_payload.get('observed_input_truth') or {})
        metadata = dict(node.metadata or {})
        if not actual_request_ref:
            actual_request_ref = str(metadata.get('latest_runtime_actual_request_ref') or '').strip()
        if not messages_ref:
            messages_ref = str(metadata.get('latest_runtime_messages_ref') or '').strip()
        if not prompt_cache_key_hash:
            prompt_cache_key_hash = str(metadata.get('latest_runtime_prompt_cache_key_hash') or '').strip()
        if not actual_request_hash:
            actual_request_hash = str(metadata.get('latest_runtime_actual_request_hash') or '').strip()
        if not actual_request_message_count:
            actual_request_message_count = int(metadata.get('latest_runtime_actual_request_message_count') or 0)
        if not actual_tool_schema_hash:
            actual_tool_schema_hash = str(metadata.get('latest_runtime_actual_tool_schema_hash') or '').strip()
        if not observed_input_truth and isinstance(metadata.get('latest_runtime_observed_input_truth'), dict):
            observed_input_truth = dict(metadata.get('latest_runtime_observed_input_truth') or {})
        ref = actual_request_ref or messages_ref
        if not ref:
            metadata = dict(node.metadata or {})
            ref = str(metadata.get('latest_runtime_messages_ref') or '').strip()
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'node_id': normalized_node_id,
            'title': str(self._repair_legacy_display_text(node.goal or node.node_id)),
            'node_kind': str(node.node_kind or 'execution'),
            'status': str(node.status or 'in_progress'),
            'updated_at': str(node.updated_at or ''),
            'ref': ref,
            'actual_request_ref': actual_request_ref,
            'messages_ref': messages_ref,
            'prompt_cache_key_hash': prompt_cache_key_hash,
            'actual_request_hash': actual_request_hash,
            'actual_request_message_count': actual_request_message_count,
            'actual_tool_schema_hash': actual_tool_schema_hash,
            'observed_input_truth': observed_input_truth,
        }

    def get_node_latest_context_payload(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        payload = self._node_latest_context_refs(task_id, node_id)
        if payload is None:
            return None
        resolver = getattr(self.log_service, 'resolve_content_ref', None)
        ref = str(payload.get('ref') or '')
        raw_content = str(resolver(ref) or '') if callable(resolver) and ref else ''
        return {**payload, 'content': self._render_node_latest_context_content(raw_content)}

    def record_node_file_change(self, task_id: str, node_id: str, *, path: str, change_type: str) -> None:
        normalized_task_id = self.normalize_task_id(task_id)
        self.log_service.record_node_file_change(
            normalized_task_id,
            node_id,
            path=path,
            change_type=change_type,
        )

    def node_detail(self, task_id: str, node_id: str, detail_level: str = 'summary') -> dict[str, Any] | str:
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_node_id = self.normalize_node_id(node_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return f'Error: Task not found: {normalized_task_id}'

        normalized_detail_level = self._normalize_node_detail_level(detail_level)
        payload = self.get_node_detail_payload(normalized_task_id, normalized_node_id, detail_level=normalized_detail_level)
        if payload is None:
            return f'Error: Node not found: {normalized_node_id}'

        artifacts = [
            {
                **artifact.model_dump(mode='json'),
                'ref': artifact_ref_from_id(artifact.artifact_id),
            }
            for artifact in self.list_artifacts(normalized_task_id)
            if str(getattr(artifact, 'node_id', '') or '').strip() == str(normalized_node_id or '').strip()
            and str(getattr(artifact, 'kind', '') or '').strip() not in {'task_execution_trace', 'task_runtime_messages'}
        ]
        artifacts_preview = artifacts[:3]
        item = payload.get('item') if isinstance(payload, dict) else None
        if isinstance(item, dict):
            payload = {
                **payload,
                'item': {
                    **item,
                    'detail_level': normalized_detail_level,
                    'artifact_count': len(artifacts),
                    'artifacts_preview': artifacts_preview if normalized_detail_level == 'summary' else [],
                },
            }
        if normalized_detail_level == 'full':
            return {
                **payload,
                'artifact_count': len(artifacts),
                'artifacts': artifacts,
            }
        return {
            **payload,
            'artifact_count': len(artifacts),
            'artifacts_preview': artifacts_preview,
        }

    @staticmethod
    def _compact_execution_trace_tool_call(step: Any) -> dict[str, Any] | None:
        return compact_tool_step_for_summary(step if isinstance(step, dict) else None)

    @staticmethod
    def _normalize_node_detail_level(detail_level: str | None) -> str:
        normalized = str(detail_level or 'summary').strip().lower()
        return normalized if normalized in {'summary', 'full'} else 'summary'

    def list_artifacts(self, task_id: str) -> list[TaskArtifactRecord]:
        task_id = self.normalize_task_id(task_id)
        return self.store.list_artifacts(task_id)

    def get_artifact(self, artifact_id: str) -> TaskArtifactRecord | None:
        return self.store.get_artifact(artifact_id)

    def describe_content(self, *, ref: str | None = None, path: str | None = None, view: str = 'canonical') -> dict[str, Any]:
        return self.content_store.describe(ref=ref, path=path, view=view)

    def search_content(
        self,
        *,
        query: str,
        ref: str | None = None,
        path: str | None = None,
        view: str = 'canonical',
        limit: int = 10,
        before: int = 2,
        after: int = 2,
    ) -> dict[str, Any]:
        return self.content_store.search(ref=ref, path=path, query=query, view=view, limit=limit, before=before, after=after)

    def open_content(
        self,
        *,
        ref: str | None = None,
        path: str | None = None,
        view: str = 'canonical',
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
    ) -> dict[str, Any]:
        return self.content_store.open(
            ref=ref,
            path=path,
            view=view,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        )

    def read_content(
        self,
        *,
        ref: str | None = None,
        path: str | None = None,
        view: str = 'canonical',
    ) -> dict[str, Any]:
        return self.content_store.read(
            ref=ref,
            path=path,
            view=view,
        )

    async def apply_patch_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        artifact = self.get_artifact(artifact_id)
        if artifact is None or artifact.task_id != task_id:
            return None
        if artifact.kind != 'patch' or not artifact.path:
            raise ValueError('artifact is not a patch artifact')
        from g3ku.agent.tools.propose_patch import parse_patch_artifact

        # 磁盘治理（P0）：统一读端，兼容 gzip artifact（patch 通常远低于压缩阈值，防阈值调低）。
        content = read_artifact_text(artifact)
        metadata, _diff_text = parse_patch_artifact(content)
        target_path = Path(str(metadata.get('path') or ''))
        old_text = base64.b64decode(str(metadata.get('old_text_b64') or '')).decode('utf-8')
        new_text = base64.b64decode(str(metadata.get('new_text_b64') or '')).decode('utf-8')
        if not target_path.exists():
            raise ValueError(f'target file not found: {target_path}')
        current = target_path.read_text(encoding='utf-8')
        if old_text not in current:
            raise ValueError('target file no longer matches patch precondition')
        if current.count(old_text) > 1:
            raise ValueError('target file has multiple matches for patch precondition')
        updated = current.replace(old_text, new_text, 1)
        target_path.write_text(updated, encoding='utf-8')
        task = self.get_task(task_id)
        self.refresh_resource_paths([target_path], trigger='artifact-apply', session_id=(task.session_id if task is not None else 'web:shared'))
        if task is not None:
            self.log_service.append_task_event(
                task_id=task.task_id,
                session_id=task.session_id,
                event_type='task.artifact.applied',
                data={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True, 'task_id': task.task_id},
            )
            self._publish_task_artifact_applied_event(
                task=task,
                artifact_id=artifact.artifact_id,
                path=str(target_path),
            )
        return {'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True}

    def _actor_role_for_node(self, node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == 'acceptance' else 'execution'

    def _node_skill_roster_supplier(self, *, task, node: NodeRecord) -> list[dict[str, str]]:
        """节点道**每跳**现算的 skill 名单（`{skill_id, display_name, description}`）。

        与 `_node_context_selection_inputs` 同一个取数口（治理实时可见集），但刻意不经过
        回合级缓存：mid-turn 新装/新授权的 skill 必须下一跳就能被点名。react_loop 没有
        service 引用，所以由 `NodeRunner._runtime_context` 把它塞进
        `live_skill_roster_supplier`，由 react_loop 每跳调用一次。

        描述保留原文（头部名单按原文渲染，注入块自己截首句）；取数异常不在这里吞——
        调用侧统一回落回合初快照，避免半份名单被当成权威可见集。
        """
        actor_role = self._actor_role_for_node(node)
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        items: list[dict[str, str]] = []
        for record in list(
            self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id) or []
        ):
            skill_id = str(getattr(record, 'skill_id', '') or '').strip()
            if not skill_id:
                continue
            items.append(
                {
                    'skill_id': skill_id,
                    'display_name': str(getattr(record, 'display_name', '') or skill_id).strip() or skill_id,
                    'description': str(getattr(record, 'description', '') or '').strip(),
                }
            )
        return items

    @staticmethod
    def _node_context_selection_cache_key(
        *,
        task: Any | None = None,
        node: Any | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> tuple[str, str] | None:
        normalized_task_id = str(task_id or getattr(task, 'task_id', '') or getattr(node, 'task_id', '') or '').strip()
        normalized_node_id = str(node_id or getattr(node, 'node_id', '') or '').strip()
        if not normalized_task_id or not normalized_node_id:
            return None
        return normalized_task_id, normalized_node_id

    def _cached_node_context_selection_entry(
        self,
        *,
        task: Any | None = None,
        node: Any | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any] | None:
        cache_key = self._node_context_selection_cache_key(
            task=task,
            node=node,
            task_id=task_id,
            node_id=node_id,
        )
        if cache_key is None:
            return None
        cache = getattr(self, '_node_context_selection_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self._node_context_selection_cache = cache
        cached = cache.get(cache_key)
        return dict(cached) if isinstance(cached, dict) else None

    def _tool_provider_selection_entry(self, *, task, node: NodeRecord) -> dict[str, Any] | None:
        live_visibility_snapshot: dict[str, Any] | None = None

        def _live_snapshot() -> dict[str, Any]:
            nonlocal live_visibility_snapshot
            if live_visibility_snapshot is None:
                live_visibility_snapshot = self._node_context_selection_live_visibility_snapshot(task=task, node=node)
            return live_visibility_snapshot

        cached = self._cached_node_context_selection_entry(task=task, node=node)
        if isinstance(cached, dict) and isinstance(cached.get('selection'), NodeContextSelectionResult):
            if (
                self._node_context_selection_entry_has_visibility_snapshot(cached)
                and self._node_context_selection_entry_is_stale(entry=cached, live_snapshot=_live_snapshot())
            ):
                self._clear_node_context_selection(task=task, node=node)
            else:
                return cached

        restored = self._restore_node_context_selection_entry(task=task, node=node)
        if restored is None:
            return None
        if (
            self._node_context_selection_entry_has_visibility_snapshot(restored)
            and self._node_context_selection_entry_is_stale(entry=restored, live_snapshot=_live_snapshot())
        ):
            return None

        cache_key = self._node_context_selection_cache_key(task=task, node=node)
        if cache_key is not None:
            cache = getattr(self, '_node_context_selection_cache', None)
            if not isinstance(cache, dict):
                cache = {}
                self._node_context_selection_cache = cache
            cache[cache_key] = restored
        return restored

    @staticmethod
    def _skill_record_value(record: Any, key: str) -> Any:
        if isinstance(record, dict):
            return record.get(key)
        return getattr(record, key, None)

    @classmethod
    def _normalized_skill_id_list(cls, records: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for record in list(records or []):
            skill_id = str(cls._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            ordered.append(skill_id)
        return ordered

    @staticmethod
    def _normalized_string_list(items: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @classmethod
    def _node_context_selection_entry_has_visibility_snapshot(cls, entry: dict[str, Any] | None) -> bool:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get('visible_tool_names'), list):
            return False
        if not isinstance(entry.get('contract_visible_skill_ids'), list):
            return False
        if not isinstance(entry.get('skill_visibility_diagnostics'), dict):
            return False
        return bool(str(entry.get('session_key') or '').strip()) and bool(str(entry.get('actor_role') or '').strip())

    @classmethod
    def _node_context_selection_entry_registry_skill_ids(cls, entry: dict[str, Any] | None) -> list[str]:
        if not isinstance(entry, dict):
            return []
        diagnostics = (
            dict(entry.get('skill_visibility_diagnostics') or {})
            if isinstance(entry.get('skill_visibility_diagnostics'), dict)
            else {}
        )
        return cls._normalized_string_list(list(diagnostics.get('registry_skill_ids') or []))

    def _node_context_selection_live_visibility_snapshot(self, *, task, node: NodeRecord) -> dict[str, Any]:
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        visible_tool_names = self._normalized_tool_name_list(
            list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_key) or [])
        )
        resource_registry = getattr(self, 'resource_registry', None)
        registry_skills = (
            list(resource_registry.list_skill_resources() or [])
            if resource_registry is not None and hasattr(resource_registry, 'list_skill_resources')
            else list(visible_skills)
        )
        return {
            'session_key': session_key,
            'actor_role': actor_role,
            'visible_tool_names': visible_tool_names,
            'contract_visible_skill_ids': self._normalized_skill_id_list(visible_skills),
            'registry_skill_ids': self._normalized_skill_id_list(registry_skills),
        }

    def _node_context_selection_entry_is_stale(
        self,
        *,
        entry: dict[str, Any] | None,
        live_snapshot: dict[str, Any],
    ) -> bool:
        if not self._node_context_selection_entry_has_visibility_snapshot(entry):
            return False
        entry = dict(entry or {})
        if str(entry.get('session_key') or '').strip() != str(live_snapshot.get('session_key') or '').strip():
            return True
        if str(entry.get('actor_role') or '').strip() != str(live_snapshot.get('actor_role') or '').strip():
            return True
        entry_visible_tool_names = self._normalized_tool_name_list(list(entry.get('visible_tool_names') or []))
        if entry_visible_tool_names != self._normalized_tool_name_list(list(live_snapshot.get('visible_tool_names') or [])):
            return True
        entry_contract_visible_skill_ids = self._normalized_string_list(list(entry.get('contract_visible_skill_ids') or []))
        if entry_contract_visible_skill_ids != self._normalized_string_list(list(live_snapshot.get('contract_visible_skill_ids') or [])):
            return True
        entry_registry_skill_ids = self._node_context_selection_entry_registry_skill_ids(entry)
        if entry_registry_skill_ids != self._normalized_string_list(list(live_snapshot.get('registry_skill_ids') or [])):
            return True
        return False

    def _skill_visibility_diagnostics(
        self,
        *,
        actor_role: str,
        session_id: str,
        visible_skills: list[Any] | None = None,
    ) -> dict[str, Any]:
        visible_skills = list(visible_skills or [])
        visible_skill_ids = set(self._normalized_skill_id_list(visible_skills))
        resource_registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        registry_skills = (
            list(resource_registry.list_skill_resources() or [])
            if resource_registry is not None and hasattr(resource_registry, 'list_skill_resources')
            else list(visible_skills)
        )
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        find_role_policy = getattr(policy_engine, '_find_role_policy', None)
        registry_skill_ids: list[str] = []
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in list(registry_skills or []):
            skill_id = str(self._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            registry_skill_ids.append(skill_id)
            allowed_roles = {
                str(role or '').strip()
                for role in list(self._skill_record_value(record, 'allowed_roles') or [])
                if str(role or '').strip()
            }
            policy_effect = ''
            if callable(find_role_policy):
                policy = find_role_policy(
                    subject=subject,
                    resource_kind='skill',
                    resource_id=skill_id,
                    action_id='load',
                )
                policy_effect = str(getattr(policy, 'effect', '') or '').strip().lower()
            entries.append(
                {
                    'skill_id': skill_id,
                    'enabled': bool(
                        True
                        if self._skill_record_value(record, 'enabled') is None
                        else self._skill_record_value(record, 'enabled')
                    ),
                    'available': bool(
                        True
                        if self._skill_record_value(record, 'available') is None
                        else self._skill_record_value(record, 'available')
                    ),
                    'allowed_for_actor_role': str(actor_role or '').strip() in allowed_roles if allowed_roles else True,
                    'policy_effect': policy_effect,
                    'included_in_contract_visible': skill_id in visible_skill_ids,
                }
            )
        return {
            'registry_skill_ids': list(registry_skill_ids),
            'entries': entries,
        }

    @staticmethod
    def _normalized_reason_values(items: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized:
                continue
            marker = normalized.lower()
            if marker in seen:
                continue
            seen.add(marker)
            ordered.append(normalized)
        return ordered

    @classmethod
    def _repair_reason_summary(cls, *, reasons: list[Any] | None, fallback: str) -> str:
        normalized = cls._normalized_reason_values(reasons)
        if normalized:
            return normalized[0]
        return str(fallback or '').strip()

    @classmethod
    def _skill_record_is_repair_required(cls, record: Any) -> bool:
        available = cls._skill_record_value(record, 'available')
        if available is None:
            return False
        return not bool(available)

    @classmethod
    def _skill_repair_reason(cls, record: Any) -> str:
        metadata = cls._skill_record_value(record, 'metadata')
        metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        reasons = [
            *list(metadata.get('errors') or []),
            *list(metadata.get('warnings') or []),
        ]
        return cls._repair_reason_summary(
            reasons=reasons,
            fallback='Repair is required before this skill can be viewed.',
        )

    def _repair_required_skill_payload(self, record: Any) -> dict[str, Any]:
        skill_id = str(self._skill_record_value(record, 'skill_id') or '').strip()
        return {
            'ok': False,
            'error': 'skill_repair_required',
            'skill_id': skill_id,
            'message': f'Skill "{skill_id}" requires repair before its body can be loaded.',
            'reference_skill': 'writing-skills',
            'warnings': list((dict(self._skill_record_value(record, 'metadata') or {}) if isinstance(self._skill_record_value(record, 'metadata'), dict) else {}).get('warnings') or []),
            'errors': list((dict(self._skill_record_value(record, 'metadata') or {}) if isinstance(self._skill_record_value(record, 'metadata'), dict) else {}).get('errors') or []),
            'next_actions': [
                'Use `exec` and `filesystem_*` tools to repair the skill files or dependencies.',
                'Reference skill `writing-skills` before retrying `load_skill_context`.',
            ],
        }

    @staticmethod
    def _normalized_tool_name_list(items: list[Any]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @staticmethod
    def _normalize_tool_call_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return dict(arguments)
        if isinstance(arguments, str):
            text = str(arguments or '').strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        if arguments is None:
            return {}
        try:
            parsed = dict(arguments)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _restore_node_context_selection_entry(self, *, task, node: NodeRecord) -> dict[str, Any] | None:
        log_service = getattr(self, 'log_service', None)
        reader = getattr(log_service, 'read_runtime_frame', None)
        task_id = str(getattr(task, 'task_id', '') or getattr(node, 'task_id', '') or '').strip()
        node_id = str(getattr(node, 'node_id', '') or '').strip()
        if not callable(reader) or not task_id or not node_id:
            return None
        frame = reader(task_id, node_id) or {}
        messages = [item for item in list(frame.get('messages') or []) if isinstance(item, dict)]
        if not messages:
            return None
        dynamic_contract_payload = extract_node_dynamic_contract_payload(messages) or {}
        required_list_fields = (
            'callable_tool_names',
            'candidate_tool_names',
            'selected_skill_ids',
            'candidate_skill_ids',
            'rbac_visible_tool_names',
            'rbac_visible_skill_ids',
        )
        missing_fields = [
            field
            for field in required_list_fields
            if field not in frame or not isinstance(frame.get(field), list)
        ]
        if missing_fields:
            raise RuntimeError(
                '运行时工具合同损坏/缺失：节点运行时 frame 缺少 canonical 合同字段 '
                + ', '.join(missing_fields)
            )
        callable_tool_names = self._normalized_tool_name_list(list(frame.get('callable_tool_names') or []))
        if not callable_tool_names:
            raise RuntimeError('运行时工具合同损坏/缺失：节点运行时 frame 的 callable_tool_names 为空')
        candidate_skill_items = [
            dict(item)
            for item in list(frame.get('candidate_skill_items') or [])
            if isinstance(item, dict)
        ]
        if not candidate_skill_items:
            candidate_skill_items = [
                dict(item)
                for item in list(dynamic_contract_payload.get('candidate_skills') or [])
                if isinstance(item, dict)
            ]
        if not candidate_skill_items:
            candidate_skill_items = [
                {
                    'skill_id': str(self._skill_record_value(item, 'skill_id') or '').strip(),
                    'description': str(self._skill_record_value(item, 'description') or '').strip(),
                }
                for item in list(frame.get('visible_skills') or [])
                if str(self._skill_record_value(item, 'skill_id') or '').strip()
            ]
        raw_candidate_skill_ids = list(frame.get('candidate_skill_ids') or [])
        candidate_skill_ids = [
            str(item or '').strip()
            for item in raw_candidate_skill_ids
            if str(item or '').strip()
        ]
        candidate_tool_names = self._normalized_tool_name_list(list(frame.get('candidate_tool_names') or []))
        candidate_tool_items = [
            dict(item)
            for item in list(frame.get('candidate_tool_items') or [])
            if isinstance(item, dict)
        ]
        if not candidate_tool_items:
            candidate_tool_items = [
                dict(item)
                for item in list(dynamic_contract_payload.get('candidate_tools') or [])
                if isinstance(item, dict)
            ]
        visible_tool_names = list(
            self.list_effective_tool_names(
                actor_role=self._actor_role_for_node(node),
                session_id=str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared',
            ) or []
        )
        raw_selected_skill_ids = list(frame.get('selected_skill_ids') or [])
        selected_skill_ids = [
            str(item or '').strip()
            for item in raw_selected_skill_ids
            if str(item or '').strip()
        ]
        if not candidate_skill_items:
            candidate_skill_items = [
                {
                    'skill_id': skill_id,
                    'description': '',
                }
                for skill_id in selected_skill_ids
            ]
        restored_selected_tool_names: list[str] = []
        for raw_name in [*list(callable_tool_names), *list(candidate_tool_names)]:
            name = str(raw_name or '').strip()
            if name and name not in restored_selected_tool_names:
                restored_selected_tool_names.append(name)
        selection = NodeContextSelectionResult(
            selected_skill_ids=selected_skill_ids,
            selected_tool_names=restored_selected_tool_names,
            candidate_skill_ids=candidate_skill_ids,
            candidate_tool_names=candidate_tool_names,
        )
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        contract_visible_skill_ids = [
            str(item or '').strip()
            for item in list(frame.get('contract_visible_skill_ids') or [])
            if str(item or '').strip()
        ]
        if not contract_visible_skill_ids:
            contract_visible_skill_ids = self._normalized_skill_id_list(visible_skills)
        skill_visibility_diagnostics = (
            dict(frame.get('skill_visibility_diagnostics') or {})
            if isinstance(frame.get('skill_visibility_diagnostics'), dict)
            else self._skill_visibility_diagnostics(
                actor_role=actor_role,
                session_id=session_key,
                visible_skills=visible_skills,
            )
        )
        return {
            'session_key': session_key,
            'actor_role': actor_role,
            'visible_skills': visible_skills,
            'contract_visible_skill_ids': contract_visible_skill_ids,
            'skill_visibility_diagnostics': skill_visibility_diagnostics,
            'candidate_tool_items': candidate_tool_items,
            'candidate_skill_items': candidate_skill_items,
            'visible_tool_records': list(self.list_visible_tools(actor_role=actor_role, session_id=session_key) or []),
            'visible_tool_names': visible_tool_names,
            'selection': selection,
        }

    def _node_context_selection_inputs(self, *, task, node: NodeRecord) -> dict[str, Any]:
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        visible_tool_records = list(self.list_visible_tools(actor_role=actor_role, session_id=session_key) or [])
        visible_tool_names = list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_key) or [])
        return {
            'session_key': session_key,
            'actor_role': actor_role,
            'visible_skills': visible_skills,
            'contract_visible_skill_ids': self._normalized_skill_id_list(visible_skills),
            'skill_visibility_diagnostics': self._skill_visibility_diagnostics(
                actor_role=actor_role,
                session_id=session_key,
                visible_skills=visible_skills,
            ),
            'visible_tool_records': visible_tool_records,
            'visible_tool_names': visible_tool_names,
        }

    async def _prepare_node_context_selection(self, *, task, node: NodeRecord) -> NodeContextSelectionResult:
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        try:
            await self.maybe_refresh_external_resource_changes(session_id=session_id)
        except Exception:
            pass
        live_visibility_snapshot: dict[str, Any] | None = None

        def _live_snapshot() -> dict[str, Any]:
            nonlocal live_visibility_snapshot
            if live_visibility_snapshot is None:
                live_visibility_snapshot = self._node_context_selection_live_visibility_snapshot(task=task, node=node)
            return live_visibility_snapshot

        cached = self._cached_node_context_selection_entry(task=task, node=node)
        if cached is not None and isinstance(cached.get('selection'), NodeContextSelectionResult):
            if (
                self._node_context_selection_entry_has_visibility_snapshot(cached)
                and self._node_context_selection_entry_is_stale(entry=cached, live_snapshot=_live_snapshot())
            ):
                self._clear_node_context_selection(task=task, node=node)
            else:
                return cached['selection']
        restored = self._restore_node_context_selection_entry(task=task, node=node)
        cache_key = self._node_context_selection_cache_key(task=task, node=node)
        if restored is not None:
            if (
                self._node_context_selection_entry_has_visibility_snapshot(restored)
                and self._node_context_selection_entry_is_stale(entry=restored, live_snapshot=_live_snapshot())
            ):
                restored = None
            elif cache_key is not None:
                cache = getattr(self, '_node_context_selection_cache', None)
                if not isinstance(cache, dict):
                    cache = {}
                    self._node_context_selection_cache = cache
                cache[cache_key] = restored
                return restored['selection']
            else:
                return restored['selection']
        inputs = self._node_context_selection_inputs(task=task, node=node)
        selection = await build_node_context_selection(
            visible_skills=list(inputs.get('visible_skills') or []),
            visible_tool_names=list(inputs.get('visible_tool_names') or []),
        )
        if cache_key is not None:
            cache = getattr(self, '_node_context_selection_cache', None)
            if not isinstance(cache, dict):
                cache = {}
                self._node_context_selection_cache = cache
            cache[cache_key] = {**inputs, 'selection': selection}
        return selection

    def _clear_node_context_selection(
        self,
        *,
        task: Any | None = None,
        node: Any | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        cache_key = self._node_context_selection_cache_key(
            task=task,
            node=node,
            task_id=task_id,
            node_id=node_id,
        )
        if cache_key is None:
            return
        cache = getattr(self, '_node_context_selection_cache', None)
        if isinstance(cache, dict):
            cache.pop(cache_key, None)

    def _tool_provider(self, node: NodeRecord) -> dict[str, Tool]:
        task = self.store.get_task(node.task_id)
        session_id = task.session_id if task is not None else 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_tool_names = list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
        selected_visible = set(
            self._callable_tool_names_for_node(
                task=(task or SimpleNamespace(task_id=node.task_id, session_id=session_id)),
                node=node,
                visible_tool_names=visible_tool_names,
            )
        )
        task_for_selection = task or SimpleNamespace(task_id=node.task_id, session_id=session_id)
        selection_entry = self._tool_provider_selection_entry(task=task_for_selection, node=node)
        selection = selection_entry.get('selection') if isinstance(selection_entry, dict) else None
        for raw_name in list(getattr(selection, 'selected_tool_names', []) or []):
            name = str(raw_name or '').strip()
            if name and name in visible_tool_names:
                selected_visible.add(name)
        for raw_name in list(getattr(selection, 'candidate_tool_names', []) or []):
            name = str(raw_name or '').strip()
            if name and name in visible_tool_names:
                selected_visible.add(name)
        # 对象字典按治理可见集兜底，而不是只按 selection/frame 回读出来的候选构建：候选与
        # 对象字典互为输入输出（候选曾= 对象字典 − 已 callable），一次塌缩就会把治理明明
        # 可见的工具变成"能加载、不提升、也调不动"的死区。
        for name in self._normalized_tool_name_list(visible_tool_names):
            if name:
                selected_visible.add(name)
        provided: dict[str, Tool] = dict(self._builtin_tool_instances(actor_role=actor_role))
        resource_manager = getattr(self, '_resource_manager', None)
        if resource_manager is not None:
            for name, tool in resource_manager.tool_instances().items():
                if name in selected_visible:
                    provided[name] = tool
        return provided

    def _builtin_tool_instances(self, *, actor_role: str) -> dict[str, Tool]:
        if str(actor_role or '').strip().lower() != 'ceo':
            return {}
        if self._builtin_tool_cache is None:
            def task_service_getter():
                return self

            self._builtin_tool_cache = {
                'stop_tool_execution': StopToolExecutionTool(
                    task_service_getter,
                ),
            }
        return dict(self._builtin_tool_cache)

    @staticmethod
    def _visible_skill_prompt_items(visible_skills: list[Any]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for record in list(visible_skills or []):
            skill_id = str(MainRuntimeService._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id:
                continue
            items.append(
                {
                    'skill_id': skill_id,
                    'display_name': str(MainRuntimeService._skill_record_value(record, 'display_name') or skill_id).strip() or skill_id,
                    'description': str(MainRuntimeService._skill_record_value(record, 'description') or '').strip(),
                }
            )
        return items

    @staticmethod
    def _tool_record_map(visible_tool_records: list[Any]) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for record in list(visible_tool_records or []):
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            if tool_id and tool_id not in mapping:
                mapping[tool_id] = record
        return mapping

    def _repair_required_tool_reason(
        self,
        *,
        tool_id: str,
        visible_tool_records: list[Any],
    ) -> str:
        try:
            toolskill_payload = dict(self.get_tool_toolskill(tool_id) or {})
        except Exception:
            toolskill_payload = {}
        reasons = [
            *list(toolskill_payload.get('errors') or []),
            *list(toolskill_payload.get('warnings') or []),
        ]
        record = self._tool_record_map(visible_tool_records).get(tool_id)
        metadata = dict(getattr(record, 'metadata', {}) or {}) if record is not None else {}
        reasons.extend(list(metadata.get('errors') or []))
        reasons.extend(list(metadata.get('warnings') or []))
        return self._repair_reason_summary(
            reasons=reasons,
            fallback='Repair is required before this tool can be used.',
        )

    def _split_repair_required_tool_prompt_items(
        self,
        *,
        candidate_tool_names: list[str],
        visible_tool_records: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
        candidate_tool_items = self._candidate_tool_prompt_items(
            candidate_tool_names=candidate_tool_names,
            visible_tool_records=visible_tool_records,
            preferred_items=preferred_items,
        )
        record_by_tool_id = self._tool_record_map(visible_tool_records)
        repair_required_items: list[dict[str, str]] = []
        remaining_names: list[str] = []
        remaining_items: list[dict[str, str]] = []
        for item in list(candidate_tool_items or []):
            tool_id = str(item.get('tool_id') or '').strip()
            if not tool_id:
                continue
            try:
                toolskill_payload = dict(self.get_tool_toolskill(tool_id) or {})
            except Exception:
                toolskill_payload = {}
            record = record_by_tool_id.get(tool_id)
            repair_required = bool(toolskill_payload.get('repair_required'))
            if not repair_required and record is not None:
                repair_required = bool(getattr(record, 'callable', True)) and not bool(getattr(record, 'available', True))
            if repair_required:
                repair_required_items.append(
                    {
                        'tool_id': tool_id,
                        'description': str(item.get('description') or '').strip(),
                        'reason': self._repair_required_tool_reason(
                            tool_id=tool_id,
                            visible_tool_records=visible_tool_records,
                        ),
                    }
                )
                continue
            remaining_names.append(tool_id)
            remaining_items.append(dict(item))
        return remaining_names, remaining_items, repair_required_items

    def _split_repair_required_skill_prompt_items(
        self,
        *,
        candidate_skill_ids: list[str],
        visible_skills: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
        candidate_skill_items = self._candidate_skill_prompt_items(
            candidate_skill_ids=candidate_skill_ids,
            visible_skills=visible_skills,
            preferred_items=preferred_items,
        )
        visible_by_skill_id = {
            str(self._skill_record_value(record, 'skill_id') or '').strip(): record
            for record in list(visible_skills or [])
            if str(self._skill_record_value(record, 'skill_id') or '').strip()
        }
        repair_required_items: list[dict[str, str]] = []
        remaining_ids: list[str] = []
        remaining_items: list[dict[str, str]] = []
        for item in list(candidate_skill_items or []):
            skill_id = str(item.get('skill_id') or '').strip()
            if not skill_id:
                continue
            record = visible_by_skill_id.get(skill_id)
            if record is not None and self._skill_record_is_repair_required(record):
                repair_required_items.append(
                    {
                        'skill_id': skill_id,
                        'description': str(item.get('description') or '').strip(),
                        'reason': self._skill_repair_reason(record),
                    }
                )
                continue
            remaining_ids.append(skill_id)
            remaining_items.append(dict(item))
        return remaining_ids, remaining_items, repair_required_items

    def _all_repair_required_tool_prompt_items(
        self,
        *,
        visible_tool_records: list[Any],
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for record in list(visible_tool_records or []):
            if not bool(getattr(record, 'callable', True)) or bool(getattr(record, 'available', True)):
                continue
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            items.append(
                {
                    'tool_id': tool_id,
                    'description': str(getattr(record, 'description', '') or '').strip(),
                    'reason': self._repair_required_tool_reason(
                        tool_id=tool_id,
                        visible_tool_records=visible_tool_records,
                    ),
                }
            )
        return items

    def _all_repair_required_skill_prompt_items(
        self,
        *,
        visible_skills: list[Any],
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for record in list(visible_skills or []):
            if not self._skill_record_is_repair_required(record):
                continue
            skill_id = str(self._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            items.append(
                {
                    'skill_id': skill_id,
                    'description': str(self._skill_record_value(record, 'description') or '').strip(),
                    'reason': self._skill_repair_reason(record),
                }
            )
        return items

    @staticmethod
    def _merge_repair_prompt_items(
        existing: list[dict[str, str]] | None,
        incoming: list[dict[str, str]] | None,
        *,
        key: str,
    ) -> list[dict[str, str]]:
        ordered: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in [*list(existing or []), *list(incoming or [])]:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get(key) or '').strip()
            if not item_key or item_key in seen:
                continue
            seen.add(item_key)
            ordered.append(dict(item))
        return ordered

    def _candidate_tool_prompt_items(
        self,
        *,
        candidate_tool_names: list[str],
        visible_tool_records: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> list[dict[str, str]]:
        record_by_tool_id = self._tool_record_map(visible_tool_records)

        preferred_by_tool_id: dict[str, dict[str, str]] = {}
        for item in list(preferred_items or []):
            if not isinstance(item, dict):
                continue
            tool_id = str(item.get('tool_id') or '').strip()
            if not tool_id or tool_id in preferred_by_tool_id:
                continue
            preferred_by_tool_id[tool_id] = {
                'tool_id': tool_id,
                'description': str(item.get('description') or '').strip(),
            }

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_name in list(candidate_tool_names or []):
            tool_id = str(raw_name or '').strip()
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            description = str((preferred_by_tool_id.get(tool_id) or {}).get('description') or '').strip()
            if not description:
                try:
                    toolskill_payload = dict(self.get_tool_toolskill(tool_id) or {})
                except Exception:
                    toolskill_payload = {}
                description = str(toolskill_payload.get('description') or toolskill_payload.get('l0') or '').strip()
            if not description:
                record = record_by_tool_id.get(tool_id)
                description = str(getattr(record, 'description', '') or '').strip() if record is not None else ''
            items.append(
                {
                    'tool_id': tool_id,
                    'description': description,
                }
            )
        return items

    def _candidate_skill_prompt_items(
        self,
        *,
        candidate_skill_ids: list[str],
        visible_skills: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> list[dict[str, str]]:
        preferred_by_skill_id: dict[str, dict[str, str]] = {}
        for item in list(preferred_items or []):
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get('skill_id') or '').strip()
            if not skill_id or skill_id in preferred_by_skill_id:
                continue
            preferred_by_skill_id[skill_id] = {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
            }

        visible_by_skill_id: dict[str, dict[str, str]] = {}
        for item in self._visible_skill_prompt_items(visible_skills):
            skill_id = str(item.get('skill_id') or '').strip()
            if not skill_id or skill_id in visible_by_skill_id:
                continue
            visible_by_skill_id[skill_id] = {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
            }

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_skill_id in list(candidate_skill_ids or []):
            skill_id = str(raw_skill_id or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            preferred_item = preferred_by_skill_id.get(skill_id) or {}
            visible_item = visible_by_skill_id.get(skill_id) or {}
            items.append(
                {
                    'skill_id': skill_id,
                    'description': (
                        str(preferred_item.get('description') or '').strip()
                        or str(visible_item.get('description') or '').strip()
                    ),
                }
            )
        return items

    def _node_exec_runtime_policy_payload(
        self,
        *,
        callable_tool_names: list[str],
        candidate_tool_names: list[str],
    ) -> dict[str, Any] | None:
        exposed_tool_names = {
            str(item or '').strip()
            for item in [*list(callable_tool_names or []), *list(candidate_tool_names or [])]
            if str(item or '').strip()
        }
        if EXEC_TOOL_ID not in exposed_tool_names:
            return None
        return self._current_exec_runtime_policy_payload()

    def _with_node_seed_contract_messages(
        self,
        *,
        task,
        node: NodeRecord,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把预置契约插在 system + payload 之后、验收尾块之前，并记下这一跳真正拿到了什么。

        一条消息恰好一个块：`_leading_block_pairs` 的块头锚定在 `lstrip()` 后的第一个字符处，
        多块同一条只有第一个被认，其余静默失效。块原文就是 `content`，前面不许垫任何说明文字。

        观测在这里写而不是照抄派发时的回执：那是节点**实际收到**的那一份。toolskill 源或授权在
        派发与首跳之间变过时，两份会不同，而这份差异只有在这里才看得见。
        """
        declared = list((getattr(node, 'metadata', None) or {}).get('seed_tools') or [])
        if not declared:
            return messages
        resolved = self.resolve_node_seed_contracts(
            task=task,
            node_kind=str(getattr(node, 'node_kind', '') or ''),
            declared=declared,
        )
        seed_messages = [dict(item) for item in list(resolved.get('messages') or []) if isinstance(item, dict)]
        # 构建期只补新出现的丢弃，派发时那一份要留着：两个时刻的 reason 合在一起才是这条名单
        # 的完整判据账，只留后半段会让「派发时就被拒」看起来像从没声明过。
        prior_dropped = [
            dict(item)
            for item in list((getattr(node, 'metadata', None) or {}).get('seeded_dropped') or [])
            if isinstance(item, dict)
        ]
        seen_dropped = {str(item.get('tool') or '').strip() for item in prior_dropped}
        dropped_lane = list(prior_dropped)
        for item in list(resolved.get('dropped') or []):
            if not isinstance(item, dict):
                continue
            if str(item.get('tool') or '').strip() in seen_dropped:
                continue
            dropped_lane.append(dict(item))
        observations = {
            'seeded_contracts': list(resolved.get('accepted') or []),
            'seeded_dropped': dropped_lane,
        }
        try:
            self.log_service.update_node_metadata(
                str(node.node_id),
                lambda current: {**dict(current or {}), **observations},
            )
        except Exception as exc:
            logger.warning('title=node_seed_observation_metadata_failed node_id={} error={}', str(node.node_id), describe_exception(exc))
        try:
            self.log_service.update_frame(
                str(getattr(task, 'task_id', '') or '').strip(),
                str(node.node_id),
                lambda frame: {**dict(frame or {}), **observations},
                publish_snapshot=False,
            )
        except Exception as exc:
            logger.warning('title=node_seed_observation_frame_failed node_id={} error={}', str(node.node_id), describe_exception(exc))
        if not seed_messages:
            return messages
        base = list(messages or [])
        insert_at = 2 if len(base) >= 2 else len(base)
        return [*base[:insert_at], *seed_messages, *base[insert_at:]]

    async def _enrich_node_messages(self, *, task, node: NodeRecord, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selection = await self._prepare_node_context_selection(task=task, node=node)
        cached = self._cached_node_context_selection_entry(task=task, node=node)
        inputs = cached or self._node_context_selection_inputs(task=task, node=node)
        visible_skills = list(inputs.get('visible_skills') or [])
        contract_visible_skill_ids = [
            str(item or '').strip()
            for item in list(inputs.get('contract_visible_skill_ids') or [])
            if str(item or '').strip()
        ]
        skill_visibility_diagnostics = (
            dict(inputs.get('skill_visibility_diagnostics') or {})
            if isinstance(inputs.get('skill_visibility_diagnostics'), dict)
            else {}
        )
        full_callable_tool_names = self._callable_tool_names_for_node(
            task=task,
            node=node,
            visible_tool_names=list(inputs.get('visible_tool_names') or []),
        )
        used_selector_selected_as_callable = not bool(full_callable_tool_names)
        if used_selector_selected_as_callable:
            full_callable_tool_names = self._normalized_tool_name_list(list(getattr(selection, 'selected_tool_names', []) or []))
        log_service = getattr(self, 'log_service', None)
        callable_tool_names = self._model_visible_callable_tool_names_for_node(
            task_id=str(getattr(task, 'task_id', '') or '').strip(),
            node_id=str(getattr(node, 'node_id', '') or '').strip(),
            node_kind=str(getattr(node, 'node_kind', '') or '').strip(),
            callable_tool_names=full_callable_tool_names,
        )
        candidate_tool_names = self._candidate_tool_names_for_node(
            task=task,
            node=node,
            selection=selection,
            visible_tool_names=list(inputs.get('visible_tool_names') or []),
        )
        raw_candidate_tool_names = self._normalized_tool_name_list(
            list(getattr(selection, 'candidate_tool_names', []) or [])
        )
        if raw_candidate_tool_names:
            record_by_tool_id = self._tool_record_map(list(inputs.get('visible_tool_records') or []))
            callable_name_set = set(callable_tool_names)
            for tool_name in raw_candidate_tool_names:
                record = record_by_tool_id.get(tool_name)
                if record is None:
                    continue
                if not bool(getattr(record, 'callable', True)) or bool(getattr(record, 'available', True)):
                    continue
                if tool_name in callable_name_set or tool_name in candidate_tool_names:
                    continue
                candidate_tool_names.append(tool_name)
        candidate_tool_names, candidate_tool_items, repair_required_tool_items = self._split_repair_required_tool_prompt_items(
            candidate_tool_names=list(candidate_tool_names),
            visible_tool_records=list(inputs.get('visible_tool_records') or []),
            preferred_items=list(inputs.get('candidate_tool_items') or []),
        )
        repair_required_tool_items = self._merge_repair_prompt_items(
            repair_required_tool_items,
            self._all_repair_required_tool_prompt_items(
                visible_tool_records=list(inputs.get('visible_tool_records') or []),
            ),
            key='tool_id',
        )
        repair_required_tool_ids = {
            str(item.get('tool_id') or '').strip()
            for item in list(repair_required_tool_items or [])
            if str(item.get('tool_id') or '').strip()
        }
        candidate_tool_names = [name for name in list(candidate_tool_names or []) if name not in repair_required_tool_ids]
        candidate_tool_items = [
            dict(item)
            for item in list(candidate_tool_items or [])
            if str(item.get('tool_id') or '').strip() not in repair_required_tool_ids
        ]
        candidate_skill_ids = list(
            getattr(selection, 'candidate_skill_ids', []) or getattr(selection, 'selected_skill_ids', []) or []
        )
        candidate_skill_ids, candidate_skill_items, repair_required_skill_items = self._split_repair_required_skill_prompt_items(
            candidate_skill_ids=candidate_skill_ids,
            visible_skills=visible_skills,
            preferred_items=list(inputs.get('candidate_skill_items') or []),
        )
        repair_required_skill_items = self._merge_repair_prompt_items(
            repair_required_skill_items,
            self._all_repair_required_skill_prompt_items(visible_skills=visible_skills),
            key='skill_id',
        )
        repair_required_skill_ids = {
            str(item.get('skill_id') or '').strip()
            for item in list(repair_required_skill_items or [])
            if str(item.get('skill_id') or '').strip()
        }
        candidate_skill_ids = [skill_id for skill_id in list(candidate_skill_ids or []) if skill_id not in repair_required_skill_ids]
        candidate_skill_items = [
            dict(item)
            for item in list(candidate_skill_items or [])
            if str(item.get('skill_id') or '').strip() not in repair_required_skill_ids
        ]
        frame_provider_tool_names: list[str] = []
        frame_reader = getattr(log_service, 'read_runtime_frame', None) if log_service is not None else None
        if callable(frame_reader):
            try:
                declared_frame = frame_reader(
                    str(getattr(task, 'task_id', '') or '').strip(),
                    str(getattr(node, 'node_id', '') or '').strip(),
                ) or {}
            except Exception:
                declared_frame = {}
            if isinstance(declared_frame, dict):
                frame_provider_tool_names = self._normalized_tool_name_list(
                    list(declared_frame.get('provider_tool_names') or [])
                )
        enriched = inject_node_dynamic_contract_message(
            self._with_node_seed_contract_messages(task=task, node=node, messages=list(messages or [])),
            NodeRuntimeToolContract(
                node_id=str(getattr(node, 'node_id', '') or '').strip(),
                node_kind=str(getattr(node, 'node_kind', '') or '').strip(),
                callable_tool_names=callable_tool_names,
                candidate_tool_names=candidate_tool_names,
                # 上一跳真正带出去的声明名单（钉住的清单只在压缩那一跳重印），尾块据此只渲
                # "能 load 但还没进 tools[]"的差集；读不到就退回渲全量候选。
                declared_tool_names=frame_provider_tool_names,
                candidate_tool_items=candidate_tool_items,
                visible_skills=[],
                candidate_skill_ids=candidate_skill_ids,
                candidate_skill_items=candidate_skill_items,
                contract_visible_skill_ids=contract_visible_skill_ids,
                skill_visibility_diagnostics=skill_visibility_diagnostics,
                repair_required_tool_items=repair_required_tool_items,
                repair_required_skill_items=repair_required_skill_items,
                lightweight_tool_ids=[],
                selection_trace={
                    'mode': 'node_context_enricher',
                    'full_callable_tool_names': list(full_callable_tool_names),
                },
                exec_runtime_policy=self._node_exec_runtime_policy_payload(
                    callable_tool_names=callable_tool_names,
                    candidate_tool_names=candidate_tool_names,
                ),
            ),
        )
        # Node catalog narrowing stays selector-driven through the memory catalog bridge.
        # Long-term MEMORY.md snapshot injection is CEO/frontdoor-only, so nodes do not
        # receive an extra retrieved-memory block here.
        return enriched

    def summary(self, session_id: str | None) -> str:
        # session_id 传 None/'' = 全局口径（跨会话），由工具层按「查询范围」参数决定。
        normalized = str(session_id or '').strip()
        return self.query_service.summary(normalized or None).text

    def get_tasks(self, session_id: str | None, task_type: int) -> str:
        normalized = str(session_id or '').strip()
        items = self.query_service.get_tasks(normalized or None, task_type)
        if not items:
            return '无匹配任务。'
        if normalized:
            return '\n'.join(f'- {item.task_id}：{item.brief}' for item in items)
        # 全局口径：逐行附状态与会话归属；in_progress 且 paused 的任务并没有真在跑，
        # 标记出来避免模型把暂停僵尸当成运行中任务转述。
        lines: list[str] = []
        for item in items:
            state = str(item.status or '')
            if state == 'in_progress' and bool(item.is_paused):
                state = 'in_progress/paused'
            lines.append(f'- {item.task_id} [{state}] ({item.session_id})：{item.brief}')
        return '\n'.join(lines)

    def failed_node_ids(self, task_id: str) -> str:
        task_id = self.normalize_task_id(task_id)
        failed_node_ids = self.query_service.failed_node_ids(task_id)
        if failed_node_ids is None:
            return f'Error: Task not found: {task_id}'
        if not failed_node_ids:
            return '无失败节点。'
        return '\n'.join(f'- {node_id}' for node_id in failed_node_ids)

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        task_id = self.normalize_task_id(task_id)
        payload = self.query_service.view_progress(task_id, mark_read=mark_read)
        if payload is None:
            return f'Error: Task not found: {task_id}'
        return payload.text

    async def close(self) -> None:
        loop_tasks = [
            self._command_poller_task,
            self._worker_heartbeat_task,
            self._node_wake_timer_task,
            self._task_disk_reconcile_task,
            self._terminal_outbox_reconcile_task,
        ]
        for task in loop_tasks:
            if task is not None and not task.done():
                task.cancel()
        if any(t is not None for t in loop_tasks):
            await asyncio.gather(
                *(task for task in loop_tasks if task is not None),
                return_exceptions=True,
            )
        delivery_tasks = [task for task in self._task_terminal_delivery_tasks.values() if task is not None and not task.done()]
        for task in delivery_tasks:
            task.cancel()
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)
        self._task_terminal_delivery_tasks.clear()
        stall_delivery_tasks = [task for task in self._task_stall_delivery_tasks.values() if task is not None and not task.done()]
        for task in stall_delivery_tasks:
            task.cancel()
        if stall_delivery_tasks:
            await asyncio.gather(*stall_delivery_tasks, return_exceptions=True)
        self._task_stall_delivery_tasks.clear()
        distribution_error_delivery_tasks = [task for task in self._task_distribution_error_delivery_tasks.values() if task is not None and not task.done()]
        for task in distribution_error_delivery_tasks:
            task.cancel()
        if distribution_error_delivery_tasks:
            await asyncio.gather(*distribution_error_delivery_tasks, return_exceptions=True)
        self._task_distribution_error_delivery_tasks.clear()
        worker_status_delivery_tasks = [task for task in self._task_worker_status_delivery_tasks.values() if task is not None and not task.done()]
        for task in worker_status_delivery_tasks:
            task.cancel()
        if worker_status_delivery_tasks:
            await asyncio.gather(*worker_status_delivery_tasks, return_exceptions=True)
        self._task_worker_status_delivery_tasks.clear()
        task_summary_flush_tasks = [task for task in self._task_summary_flush_tasks.values() if task is not None and not task.done()]
        for task in task_summary_flush_tasks:
            task.cancel()
        if task_summary_flush_tasks:
            await asyncio.gather(*task_summary_flush_tasks, return_exceptions=True)
        self._task_summary_flush_tasks.clear()
        if self._pending_task_summaries:
            for task_id in list(self._pending_task_summaries.keys()):
                self._flush_task_summary_to_outbox(task_id)
        task_summary_delivery_task = self._task_summary_delivery_task
        if task_summary_delivery_task is not None and not task_summary_delivery_task.done():
            task_summary_delivery_task.cancel()
            await asyncio.gather(task_summary_delivery_task, return_exceptions=True)
        self._task_summary_delivery_task = None
        flush_task = self._task_event_flush_task
        self._task_event_flush_task = None
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
            await asyncio.gather(flush_task, return_exceptions=True)
        # 队列里没来得及投的实时补丁随进程一起丢掉，不跨重启补投：它们是"最新覆盖"的
        # 视图刷新，下一个补丁自然补齐；任务收口走持久车道 task_terminal_outbox。
        self._task_event_pending.clear()
        callback_client = self._callback_client
        self._callback_client = None
        if callback_client is not None:
            await callback_client.aclose()
        if self.tool_pressure_monitor is not None:
            await self.tool_pressure_monitor.close()
        if self.node_turn_controller is not None:
            await self.node_turn_controller.close()
        await self.worker_heartbeat_service.close()
        await self.task_stall_notifier.close()
        await self.node_watchdog.close()
        if self.execution_mode == 'worker' and self.worker_id and self._worker_lease_acquired:
            stopped_item = {
                'worker_id': self.worker_id,
                'role': 'task_worker',
                'status': 'stopped',
                'updated_at': now_iso(),
                'payload': {'execution_mode': self.execution_mode, 'worker_heartbeat_at': now_iso(), 'debug': {'recent_long_blocks': self.runtime_debug_recorder.snapshot()}, **self._tool_pressure_snapshot()},
            }
            self.store.upsert_worker_status(
                worker_id=str(stopped_item['worker_id']),
                role=str(stopped_item['role']),
                status=str(stopped_item['status']),
                updated_at=str(stopped_item['updated_at']),
                payload=dict(stopped_item['payload']),
            )
            self.publish_worker_status_event(item=stopped_item)
            self.store.release_worker_lease(role=_WORKER_LEASE_ROLE, worker_id=str(self.worker_id))
            self._worker_lease_acquired = False
        await self.global_scheduler.close()
        self.log_service.close()
        await self.registry.close()
        self.governance_store.close()
        self.store.close()

    def _tool_pressure_snapshot(self) -> dict[str, Any]:
        summary_stats = self._summary_stats_snapshot()
        node_turn_snapshot = self._node_turn_snapshot()
        model_route_snapshot = self._model_route_snapshot()
        if self.tool_pressure_monitor is not None:
            return {
                **dict(self.tool_pressure_monitor.snapshot() or {}),
                **node_turn_snapshot,
                **model_route_snapshot,
                **summary_stats,
            }
        if self.adaptive_tool_budget_controller is not None:
            return {
                **dict(self.adaptive_tool_budget_controller.snapshot() or {}),
                **node_turn_snapshot,
                **model_route_snapshot,
                **summary_stats,
            }
        return {**node_turn_snapshot, **model_route_snapshot, **summary_stats}

    def _model_route_snapshot(self) -> dict[str, Any]:
        """负载均衡器的紧凑心跳快照，随 worker 状态上报。

        balancer 是 worker 进程内的运行态，web 进程读不到它的内存；把这些数字挂在心跳
        载荷上，管理面才能跨进程看到。只给计数与桶序号——桶身份是不可逆摘要，也不外泄。
        """
        balancer = getattr(self, 'model_load_balancer', None)
        if balancer is None:
            return {'model_route_groups': []}
        try:
            raw = dict(balancer.snapshot() or {})
        except Exception:
            return {'model_route_groups': []}
        groups: list[dict[str, Any]] = []
        for group_key, payload in dict(raw.get('groups') or {}).items():
            members = []
            for member in list(payload.get('members') or [])[:_MODEL_ROUTE_SNAPSHOT_MAX_MEMBERS]:
                members.append(
                    {
                        'model_key': member.get('model_key'),
                        'running': int(member.get('running') or 0),
                        'waiting': int(member.get('waiting') or 0),
                        'reserved': int(member.get('reserved') or 0),
                        'rolling_rpm_60s': int(member.get('rolling_rpm_60s') or 0),
                        'penalty_429': float(member.get('penalty_429') or 0.0),
                        'score': float(member.get('score') or 0.0),
                        # None = 该成员没配 per-key 上限，本地容量无限。
                        'local_capacity': member.get('local_capacity'),
                        'selectable': bool(member.get('selectable')),
                        'quota_bucket_index': int(member.get('quota_bucket_index') or 0),
                    }
                )
            groups.append(
                {
                    'group_key': str(group_key),
                    'enabled': bool(payload.get('enabled')),
                    'max_retry_rounds': int(payload.get('max_retry_rounds') or 1),
                    'quota_bucket_count': int(payload.get('quota_bucket_count') or 0),
                    'shared_bucket_member_count': int(payload.get('shared_bucket_member_count') or 0),
                    'unresolved_bucket_count': int(payload.get('unresolved_bucket_count') or 0),
                    'members': members,
                }
            )
        return {
            'model_route_groups': groups,
            'model_route_config_revision': int(raw.get('config_revision') or 0),
            'model_route_binding_count': len(list(raw.get('node_bindings') or [])),
            # 组表漂移：注册键与组自带 group_key 不一致、或一颗成员被两个组同时认领。
            # 非 0 就意味着日志里的 group= 与台账的档位归因不再可信，必须先查它。
            'model_route_group_drift_count': int(raw.get('group_drift_count') or 0),
            'model_route_group_drift_findings': list(raw.get('group_drift_findings') or []),
        }

    def _node_turn_snapshot(self) -> dict[str, Any]:
        controller = getattr(self, 'node_turn_controller', None)
        if controller is None:
            return {
                'node_queue_running_count': 0,
                'node_queue_waiting_count': 0,
                'node_queue_oldest_wait_ms': 0.0,
            }
        try:
            payload = dict(controller.snapshot() or {})
        except Exception:
            payload = {}
        return {
            'node_queue_running_count': int(payload.get('node_queue_running_count') or 0),
            'node_queue_waiting_count': int(payload.get('node_queue_waiting_count') or 0),
            'node_queue_frozen_count': int(payload.get('node_queue_frozen_count') or 0),
            'node_queue_oldest_wait_ms': float(payload.get('node_queue_oldest_wait_ms') or 0.0),
            # 队头读数：`running=0` 而 `waiting>0` 持续累积时，这两个数直接说出"谁把队列
            # 钉住、为什么"，不必再去挖日志（2026-10-12 那次 0 次授予、零错误行、110 分钟）。
            'node_queue_head_pinned_seconds': float(payload.get('node_queue_head_pinned_seconds') or 0.0),
            'node_queue_head_busy_reason': str(payload.get('node_queue_head_busy_reason') or ''),
            'node_queue_head_node_id': str(payload.get('node_queue_head_node_id') or ''),
            'node_queue_head_required_window': int(payload.get('node_queue_head_required_window') or 0),
        }

    def _node_turn_gate_allowed(self) -> bool:
        monitor = getattr(self, 'tool_pressure_monitor', None)
        if monitor is None:
            return True
        try:
            snapshot = dict(monitor.snapshot() or {})
        except Exception:
            return True
        # D) Staleness fail-open: never hold node turns on a stale critical state if the
        # sampler thread stops producing. Use sample AGE, not pressure_snapshot_fresh
        # (which is False whenever machine metrics are unavailable even when the sampler
        # thread is healthy).
        stale_after_seconds = float(getattr(self, '_pressure_gate_stale_after_seconds', 10.0) or 0.0)
        if stale_after_seconds > 0.0:
            raw_age_ms = snapshot.get('pressure_sample_age_ms')
            try:
                sample_age_ms = float(raw_age_ms) if raw_age_ms is not None else float('inf')
            except (TypeError, ValueError):
                sample_age_ms = float('inf')
            if sample_age_ms > stale_after_seconds * 1000.0:
                return True
        # E) Only LOCAL critical closes the turn gate (slow down, don't freeze); machine
        # pressure squeezes only the tool budget. Legacy behavior behind a toggle.
        local_state = str(snapshot.get('local_pressure_state') or '').strip().lower()
        if local_state == 'critical':
            return False
        if bool(getattr(self, '_pressure_gate_close_on_machine_critical', False)):
            budget_state = str(snapshot.get('budget_state') or snapshot.get('tool_pressure_state') or 'normal').strip().lower()
            machine_state = str(snapshot.get('machine_pressure_state') or '').strip().lower()
            return budget_state != 'critical' and machine_state != 'critical'
        return True








    def _resolve_quota_buckets(self, model_ref: str) -> list[str]:
        """解析某条 binding 的配额身份桶（endpoint + api key，或显式 quota_pool_key）。

        只在 worker 进程内调用：密钥材料要靠解锁后的 secret payload 才拿得到，未解锁或
        解析不到时返回空列表，由 balancer 按「身份未知」处理，绝不把不同成员并进同一个
        空桶。返回值是不可逆摘要，任何日志与管理面输出都只给桶序号。
        """
        key = str(model_ref or "").strip()
        config = getattr(self, "_app_config", None)
        if not key or config is None:
            return []
        managed = None
        try:
            managed = config.get_managed_model(key)
        except Exception:
            managed = None
        pool_key = str(getattr(managed, "quota_pool_key", "") or "").strip() if managed is not None else ""
        if pool_key:
            return [quota_bucket_key(endpoint="", api_key="", quota_pool_key=pool_key)]
        try:
            target = resolve_chat_target(config, key, workspace=config.workspace_path)
        except Exception:
            return []
        endpoint = str(getattr(target, "base_url", "") or "").strip()
        raw_api_key = str((dict(getattr(target, "secret_payload", {}) or {})).get("api_key", "") or "")
        buckets: list[str] = []
        for api_key in parse_api_keys(raw_api_key):
            bucket = quota_bucket_key(endpoint=endpoint, api_key=str(api_key or "").strip())
            if bucket and bucket not in buckets:
                buckets.append(bucket)
        return buckets

    def _apply_model_route_plans(self, execution_routes: Any, inspection_routes: Any, *, revision: int) -> None:
        """route plan 与组定义必须同一时刻落地。

        准入层对链上每个 load_balance entry 都要能在 balancer 里查到那个组，查不到就以
        unknown_group 跳过整条链，节点永远拿不到授予。所以任何写入 node_runner route plan
        的路径（启动构造与配置刷新）都必须同时把同一份组喂给 balancer。
        config_revision 只作观测，重绑只发生在「绑定成员已被移出组」的那些节点上，
        在飞请求与其余亲和状态不受影响。
        """
        self.node_runner._execution_model_routes = execution_routes
        self.node_runner._acceptance_model_routes = inspection_routes
        if self.model_load_balancer is not None:
            self.model_load_balancer.configure(
                groups=self._load_balance_groups_from_plans(),
                config_revision=int(revision or 0),
            )

    def _initial_model_routes(self, config: Any, scope: str) -> Any:
        """启动时解析 route plan。config 不可用时返回 None，让 refs 视图继续驱动。"""
        if config is None:
            return None
        try:
            plan = build_model_route_plan(config, scope, revision=0)
        except Exception:
            return None
        if scope == 'inspection' and not list(getattr(plan, 'routes', []) or []):
            try:
                return build_model_route_plan(config, 'execution', revision=0)
            except Exception:
                return plan
        return plan

    def _load_balance_groups_from_plans(self) -> dict[str, Any]:
        """从已解析的 route plan 里取组状态喂给 balancer，保证两侧看到的是同一份定义。"""
        groups: dict[str, Any] = {}
        for plan in (
            getattr(self.node_runner, "_execution_model_routes", None),
            getattr(self.node_runner, "_acceptance_model_routes", None),
        ):
            if plan is None:
                continue
            for route in list(getattr(plan, "routes", []) or []):
                if not bool(getattr(route, "is_load_balance", False)):
                    continue
                group = getattr(route, "group", None)
                if group is None:
                    continue
                groups[str(route.group_key)] = group
        return groups

    def _resolve_model_limit_payload(self, model_ref: str) -> dict[str, Any]:
        config = self._app_config
        normalized_model_ref = str(model_ref or '').strip()
        if config is None or not normalized_model_ref:
            return {'key_count': 1, 'key_indexes': [0], 'per_key_limits': {0: None}}
        try:
            target = resolve_chat_target(config, normalized_model_ref, workspace=config.workspace_path)
        except Exception:
            return {'key_count': 1, 'key_indexes': [0], 'per_key_limits': {0: None}}
        raw_api_key = str((dict(getattr(target, 'secret_payload', {}) or {})).get('api_key', '') or '')
        key_count = max(1, len(parse_api_keys(raw_api_key)))
        layout = resolve_api_key_concurrency_layout(
            raw_api_key,
            getattr(target, 'single_api_key_max_concurrency', None),
            include_empty_slot=True,
            reject_all_zero=False,
        )
        key_indexes = list(layout.key_indexes) or [0]
        per_key_limits = {index: None for index in key_indexes}
        for index, limit in zip(layout.key_indexes, layout.key_limits):
            per_key_limits[int(index)] = limit
        return {
            'key_count': key_count,
            'key_indexes': key_indexes,
            'per_key_limits': per_key_limits,
        }

    def _tool_pressure_status_payload(self, item: dict[str, Any] | None) -> dict[str, Any]:
        current_payload = dict(item.get('payload') or {}) if isinstance(item, dict) else {}
        live_snapshot = self._tool_pressure_snapshot() if self.execution_mode == 'worker' else {}
        merged = {**current_payload, **live_snapshot}
        summary_stats = self._summary_stats_snapshot()
        for key in list(summary_stats.keys()):
            if key in merged:
                try:
                    summary_stats[key] = float(merged.get(key) or 0.0)
                except Exception:
                    summary_stats[key] = 0.0
        sample_at = str(merged.get('pressure_sample_at') or merged.get('tool_pressure_sample_at') or '')
        sample_age_ms: float | None
        sample_dt = self._parse_worker_timestamp(sample_at)
        if sample_dt is None:
            sample_age_ms = None
        else:
            sample_age_ms = max(0.0, (datetime.now(timezone.utc) - sample_dt).total_seconds() * 1000.0)
        stale_after_seconds = float(
            self._adaptive_tool_budget_settings(self._app_config).get('pressure_snapshot_stale_after_seconds', 3.0)
        )
        pressure_snapshot_fresh = bool(
            sample_age_ms is not None
            and bool(merged.get('machine_pressure_available'))
            and sample_age_ms <= (stale_after_seconds * 1000.0)
        )
        heartbeat_at = str(merged.get('worker_heartbeat_at') or (item.get('updated_at') if isinstance(item, dict) else '') or '')
        heartbeat_dt = self._parse_worker_timestamp(heartbeat_at)
        if heartbeat_dt is None:
            heartbeat_age_ms = None
        else:
            heartbeat_age_ms = max(0.0, (datetime.now(timezone.utc) - heartbeat_dt).total_seconds() * 1000.0)
        heartbeat_fresh = bool(
            heartbeat_age_ms is not None
            and heartbeat_age_ms <= (self._worker_status_stale_after_seconds(item if isinstance(item, dict) else None) * 1000.0)
        )
        return {
            'tool_pressure_state': str(merged.get('tool_pressure_state') or 'normal'),
            'tool_pressure_target_limit': int(merged.get('tool_pressure_target_limit') or 0),
            'tool_pressure_running_count': int(merged.get('tool_pressure_running_count') or 0),
            'tool_pressure_waiting_count': int(merged.get('tool_pressure_waiting_count') or 0),
            'tool_queue_running_count': int(merged.get('tool_queue_running_count') or merged.get('tool_pressure_running_count') or 0),
            'tool_queue_waiting_count': int(merged.get('tool_queue_waiting_count') or merged.get('tool_pressure_waiting_count') or 0),
            'tool_pressure_event_loop_lag_ms': float(merged.get('tool_pressure_event_loop_lag_ms') or 0.0),
            'tool_pressure_writer_queue_depth': int(merged.get('tool_pressure_writer_queue_depth') or 0),
            'tool_pressure_process_cpu_ratio': float(merged.get('tool_pressure_process_cpu_ratio') or 0.0),
            'tool_pressure_last_transition_at': str(merged.get('tool_pressure_last_transition_at') or ''),
            'tool_pressure_throttled_since': str(merged.get('tool_pressure_throttled_since') or ''),
            'tool_pressure_critical_since': str(merged.get('tool_pressure_critical_since') or ''),
            'tool_pressure_self_heal_reason': str(merged.get('tool_pressure_self_heal_reason') or ''),
            'worker_execution_state': str(merged.get('worker_execution_state') or merged.get('tool_pressure_state') or 'normal'),
            'worker_execution_target_limit': int(merged.get('worker_execution_target_limit') or merged.get('tool_pressure_target_limit') or 0),
            'worker_execution_running_count': int(merged.get('worker_execution_running_count') or merged.get('tool_pressure_running_count') or 0),
            'worker_execution_waiting_count': int(merged.get('worker_execution_waiting_count') or merged.get('tool_pressure_waiting_count') or 0),
            'worker_execution_oldest_wait_ms': float(merged.get('worker_execution_oldest_wait_ms') or 0.0),
            'node_queue_running_count': int(merged.get('node_queue_running_count') or 0),
            'node_queue_waiting_count': int(merged.get('node_queue_waiting_count') or 0),
            'node_queue_oldest_wait_ms': float(merged.get('node_queue_oldest_wait_ms') or 0.0),
            # 队头被钉住时，界面要能读出"钉住的是谁、为什么"，否则这条车道只能靠取证。
            'node_queue_head_pinned_seconds': float(merged.get('node_queue_head_pinned_seconds') or 0.0),
            'node_queue_head_busy_reason': str(merged.get('node_queue_head_busy_reason') or ''),
            'node_queue_head_node_id': str(merged.get('node_queue_head_node_id') or ''),
            'node_queue_head_required_window': int(merged.get('node_queue_head_required_window') or 0),
            # 回合闸：面板上「等待请求位」的那个数是闸口排队，不是 node_queue_waiting
            # （后者过闸之后才计）。这些键只活在 worker 心跳的 raw payload 里时，
            # 顶层契约与前端都读不到，闸贴顶排队的窗口在界面上就显示成 0 等待。
            'entry_gate_queued': dict(merged.get('entry_gate_queued') or {}),
            'entry_gate_queued_total': int(merged.get('entry_gate_queued_total') or 0),
            'entry_gate_running_total': int(merged.get('entry_gate_running_total') or 0),
            'entry_gate_limits': dict(merged.get('entry_gate_limits') or {}),
            'entry_gate_limits_total': int(merged.get('entry_gate_limits_total') or 0),
            'entry_gate_targets': dict(merged.get('entry_gate_targets') or {}),
            # 共享池本体与各角色保留格：容量不再按角色拆开，判读"谁在等、还能放几格"
            # 要靠这两份，而不是把两个角色的 limit 相加（那会把同一个池数读两遍）。
            'entry_gate_pool': dict(merged.get('entry_gate_pool') or {}),
            'entry_gate_reserved': dict(merged.get('entry_gate_reserved') or {}),
            'entry_completions_60s': float(merged.get('entry_completions_60s') or 0.0),
            'entry_completions_at_last_growth': merged.get('entry_completions_at_last_growth'),
            'entry_binding_seconds': float(merged.get('entry_binding_seconds') or 0.0),
            # 负载均衡：web 进程只能看到 worker 心跳里带上来的快照（balancer 是进程内态）。
            'model_route_groups': list(merged.get('model_route_groups') or []),
            'model_route_config_revision': int(merged.get('model_route_config_revision') or 0),
            'model_route_binding_count': int(merged.get('model_route_binding_count') or 0),
            'machine_pressure_state': str(merged.get('machine_pressure_state') or 'unknown'),
            'local_pressure_state': str(merged.get('local_pressure_state') or 'unknown'),
            'budget_state': str(merged.get('budget_state') or merged.get('tool_pressure_state') or 'normal'),
            'machine_pressure_available': bool(merged.get('machine_pressure_available')),
            'machine_pressure_cpu_percent': float(merged.get('machine_pressure_cpu_percent') or 0.0),
            'machine_pressure_memory_percent': float(merged.get('machine_pressure_memory_percent') or 0.0),
            'machine_pressure_disk_busy_percent': float(merged.get('machine_pressure_disk_busy_percent') or 0.0),
            'machine_pressure_disk_busy_available': bool(merged.get('machine_pressure_disk_busy_available')),
            'machine_pressure_disk_read_bytes_per_sec': float(merged.get('machine_pressure_disk_read_bytes_per_sec') or 0.0),
            'machine_pressure_disk_write_bytes_per_sec': float(merged.get('machine_pressure_disk_write_bytes_per_sec') or 0.0),
            # 磁盘治理（P1）：剩余空间水位与紧急态（前端性能条与横幅消费）。
            'machine_disk_free_bytes': int(merged.get('machine_disk_free_bytes') if merged.get('machine_disk_free_bytes') is not None else -1),
            'machine_disk_total_bytes': int(merged.get('machine_disk_total_bytes') if merged.get('machine_disk_total_bytes') is not None else -1),
            'machine_disk_usage_percent': float(merged.get('machine_disk_usage_percent') or 0.0),
            'disk_emergency_active': bool(merged.get('disk_emergency_active')),
            'disk_emergency_since': str(merged.get('disk_emergency_since') or ''),
            'sqlite_write_wait_ms': float(merged.get('sqlite_write_wait_ms') or 0.0),
            'sqlite_query_latency_ms': float(merged.get('sqlite_query_latency_ms') or 0.0),
            'pressure_sample_at': sample_at,
            'pressure_sample_age_ms': round(sample_age_ms, 3) if sample_age_ms is not None else None,
            'pressure_snapshot_fresh': pressure_snapshot_fresh,
            'worker_heartbeat_at': heartbeat_at,
            'worker_heartbeat_age_ms': round(heartbeat_age_ms, 3) if heartbeat_age_ms is not None else None,
            'worker_heartbeat_fresh': heartbeat_fresh,
            **summary_stats,
            'debug': dict(merged.get('debug') or {}) if isinstance(merged.get('debug'), dict) else {},
        }

    def perf_report(self, *, mode: Any = '', window_minutes: Any = None) -> str:
        normalized_mode = str(mode or 'window').strip().lower()
        if normalized_mode not in {'live', 'window'}:
            return (
                "perf_inspect accepts mode='window' (sampled history, the default) or "
                "mode='live' (current worker snapshot plus that same window)."
            )
        if window_minutes in (None, ''):
            window = _PERF_REPORT_DEFAULT_WINDOW_MINUTES
        else:
            parsed_window = _perf_number(window_minutes)
            if parsed_window is None:
                return 'perf_inspect window_minutes must be a number of minutes between 1 and 1440.'
            window = max(1.0, min(parsed_window, _PERF_REPORT_MAX_WINDOW_MINUTES))
        end_at = datetime.now(timezone.utc)
        rows = self._perf_sample_rows(start_at=end_at - timedelta(minutes=window), end_at=end_at)
        if normalized_mode == 'live':
            return self._render_perf_live_report(rows=rows, window_minutes=window)
        return self._render_perf_window_report(rows=rows, window_minutes=window)

    def _perf_sample_rows(self, *, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        raw = self.store.list_perf_samples(
            since_iso=start_at.astimezone().isoformat(timespec='seconds'),
            until_iso=end_at.astimezone().isoformat(timespec='seconds'),
            limit=_PERF_REPORT_MAX_ROWS,
        )
        rows: list[dict[str, Any]] = []
        for item in raw:
            stamp = self._perf_sample_time(item.get('sampled_at'))
            if stamp is None or stamp < start_at or stamp > end_at:
                continue
            payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
            rows.append({'sampled_at': str(item.get('sampled_at') or ''), 'stamp': stamp, 'payload': payload})
        return rows

    @staticmethod
    def _perf_sample_time(value: Any) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone(timezone.utc)

    def _perf_sample_stats(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        stamps = [row['stamp'] for row in rows]
        steps = [(stamps[index + 1] - stamps[index]).total_seconds() for index in range(len(stamps) - 1)]
        median_step = _perf_percentile(steps, 0.5) if steps else _PERF_SAMPLE_INTERVAL_FALLBACK_SECONDS
        tier_seconds: dict[str, float] = {}
        tier_runs: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            owned = steps[index] if index < len(steps) else median_step
            state = str(row['payload'].get('budget_state') or 'normal').strip().lower() or 'normal'
            tier_seconds[state] = tier_seconds.get(state, 0.0) + owned
            run_until = stamps[index + 1] if index + 1 < len(stamps) else stamps[index]
            if tier_runs and str(tier_runs[-1].get('state') or '') == state:
                tier_runs[-1]['until'] = run_until
                tier_runs[-1]['seconds'] = float(tier_runs[-1].get('seconds') or 0.0) + owned
            else:
                tier_runs.append({'state': state, 'since': stamps[index], 'until': run_until, 'seconds': owned})
        gap_threshold = max(45.0, median_step * 3.0)
        gaps = [
            {'since': stamps[index], 'until': stamps[index + 1], 'seconds': step}
            for index, step in enumerate(steps)
            if step > gap_threshold
        ]
        axes: dict[str, dict[str, float]] = {}
        for label, key, kind in _PERF_STAT_AXES:
            values: list[float] = []
            for row in rows:
                payload = row['payload']
                if label == 'disk' and not bool(payload.get('machine_pressure_disk_busy_available', True)):
                    continue
                numeric = _perf_number(payload.get(key), drop_negative=kind == 'bytes')
                if numeric is None:
                    continue
                values.append(numeric)
            if not values:
                continue
            axes[label] = {
                'max': max(values),
                'min': min(values),
                'p50': _perf_percentile(values, 0.5),
                'last': values[-1],
            }
        restricted = sum(seconds for state, seconds in tier_seconds.items() if state != 'normal')
        disk_emergency_seconds = sum(
            (steps[index] if index < len(steps) else median_step)
            for index, row in enumerate(rows)
            if bool(row['payload'].get('disk_emergency_active'))
        )
        return {
            'sample_count': len(rows),
            'first': stamps[0] if stamps else None,
            'last': stamps[-1] if stamps else None,
            'median_step': median_step,
            'tier_seconds': tier_seconds,
            'tier_runs': tier_runs,
            'gaps': gaps,
            'gap_threshold': gap_threshold,
            'axes': axes,
            'restricted_seconds': restricted,
            'covered_seconds': sum(tier_seconds.values()),
            'machine_available_samples': sum(1 for row in rows if bool(row['payload'].get('machine_pressure_available'))),
            'disk_emergency_seconds': disk_emergency_seconds,
        }

    def _perf_series_buckets(self, rows: list[dict[str, Any]], *, window_minutes: float) -> list[dict[str, Any]]:
        if not rows:
            return []
        bucket_seconds = max(60.0, (float(window_minutes) * 60.0) / float(_PERF_REPORT_MAX_BUCKETS))
        origin = rows[0]['stamp']
        buckets: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for row in rows:
            index = int((row['stamp'] - origin).total_seconds() // bucket_seconds)
            if current is None or int(current['index']) != index:
                current = {
                    'index': index,
                    'since': row['stamp'],
                    'peaks': {},
                    'state': 'normal',
                    'state_rank': _PERF_TIER_ORDER.index('normal'),
                }
                buckets.append(current)
            payload = row['payload']
            for label in _PERF_SERIES_LABELS:
                numeric = _perf_number(payload.get(_PERF_AXIS_KEY.get(label, '')))
                if numeric is None:
                    continue
                peaks = current['peaks']
                peaks[label] = max(numeric, float(peaks.get(label) or 0.0))
            state = str(payload.get('budget_state') or 'normal').strip().lower() or 'normal'
            rank = _PERF_TIER_ORDER.index(state) if state in _PERF_TIER_ORDER else -1
            if rank >= int(current['state_rank']):
                current['state_rank'] = rank
                current['state'] = state
        return buckets

    def _perf_axis_lines(self, stats: dict[str, Any]) -> list[str]:
        axes = stats['axes']
        # 缺轴直接不出现，不用 '--' 占位：占位会被读成"测到了 0"。
        lines: list[str] = []
        for title, labels in _PERF_GROUPED_AXES:
            parts = [
                _perf_axis_text(label, _PERF_AXIS_KIND[label], axes[label])
                for label in labels
                if label in axes
            ]
            if parts:
                lines.append(f'{title}: ' + ' | '.join(parts))
        if stats['machine_available_samples'] and stats['machine_available_samples'] < stats['sample_count']:
            lines.append(
                f"Machine metrics unavailable in {stats['sample_count'] - stats['machine_available_samples']}"
                f"/{stats['sample_count']} samples (cpu/mem/disk read as 0 there, not measured)."
            )
        return lines

    def _render_perf_window_report(self, *, rows: list[dict[str, Any]], window_minutes: float) -> str:
        lines = [f'Perf window: {window_minutes:g}m requested.']
        if not rows:
            lines.append('Samples=0 means nothing was recorded in this window, NOT that the machine was idle.')
            lines.extend(self._perf_coverage_lines())
            return '\n'.join(lines)
        stats = self._perf_sample_stats(rows)
        lines.append(
            f"Samples: {stats['sample_count']} from {_perf_clock_text(stats['first'])} to {_perf_clock_text(stats['last'])}, "
            f"step={_perf_seconds_text(stats['median_step'])}."
        )
        covered = float(stats['covered_seconds'] or 0.0)
        share = (float(stats['restricted_seconds']) / covered * 100.0) if covered > 0.0 else 0.0
        lines.append(f"Tiers: {_perf_tiers_text(stats['tier_seconds'])} | restricted_share={share:.0f}% of sampled time.")
        runs = [run for run in stats['tier_runs'] if str(run['state']) != 'normal']
        if runs:
            rendered = [
                f"{run['state']} {_perf_clock_text(run['since'])}->{_perf_clock_text(run['until'])} ({_perf_seconds_text(run['seconds'])})"
                for run in runs[-8:]
            ]
            lines.append('Pressure runs: ' + '; '.join(rendered))
        else:
            lines.append('Pressure runs: none - the tool budget stayed unrestricted for the whole window.')
        lines.extend(self._perf_axis_lines(stats))
        if float(stats['disk_emergency_seconds']) > 0.0:
            lines.append(
                f"Disk emergency: held for {_perf_seconds_text(stats['disk_emergency_seconds'])} - the tool budget was "
                'hard-gated to zero there, so waiting calls were waiting on disk, not on the model.'
            )
        if stats['gaps']:
            rendered = [
                f"{_perf_clock_text(gap['since'])}->{_perf_clock_text(gap['until'])} ({_perf_seconds_text(gap['seconds'])})"
                for gap in stats['gaps'][-10:]
            ]
            lines.append(
                f"Sampling gaps > {_perf_seconds_text(stats['gap_threshold'])}: {len(stats['gaps'])} ({'; '.join(rendered)}). "
                'A gap means the worker recorded nothing there, so it cannot be read as idle.'
            )
        buckets = self._perf_series_buckets(rows, window_minutes=window_minutes)
        lines.append('Series (worst value per bucket): ' + ' '.join(['ts'] + list(_PERF_SERIES_LABELS) + ['state']))
        for bucket in buckets:
            peaks = bucket['peaks']
            cells = [_perf_clock_text(bucket['since'])]
            for label in _PERF_SERIES_LABELS:
                value = peaks.get(label)
                cells.append('--' if value is None else f'{value:.0f}')
            cells.append(str(bucket['state']))
            lines.append(' '.join(cells))
        return '\n'.join(lines)

    def _render_perf_live_report(self, *, rows: list[dict[str, Any]], window_minutes: float) -> str:
        payload = self.worker_status_payload()
        lines = ['Perf now (live worker snapshot):']
        lines.append(
            f"Worker: state={payload.get('worker_state')} last_heartbeat={payload.get('worker_last_seen_at') or '-'} "
            f"heartbeat_age_ms={payload.get('worker_heartbeat_age_ms')} snapshot_fresh={payload.get('pressure_snapshot_fresh')}"
        )
        if not payload.get('machine_pressure_available'):
            lines.append('Machine: not measured (machine_pressure_available=false) - do not read the zeroed cpu/mem/disk as idle.')
        free_bytes = _perf_number(payload.get('machine_disk_free_bytes'), drop_negative=True)
        lines.append(
            "Queues: "
            f"tool_running={payload.get('tool_queue_running_count')}/{payload.get('tool_pressure_target_limit')} "
            f"tool_waiting={payload.get('tool_queue_waiting_count')} "
            f"oldest_tool_wait={_perf_duration_text(payload.get('worker_execution_oldest_wait_ms') or 0)} "
            f"node_running={payload.get('node_queue_running_count')} "
            f"node_waiting={payload.get('node_queue_waiting_count')} "
            f"node_oldest_wait={_perf_duration_text(payload.get('node_queue_oldest_wait_ms') or 0)}"
        )
        lines.append(
            f"Machine: cpu={payload.get('machine_pressure_cpu_percent')}% mem={payload.get('machine_pressure_memory_percent')}% "
            f"disk_busy={payload.get('machine_pressure_disk_busy_percent')}% "
            f"disk_free={_perf_bytes_text(free_bytes) if free_bytes is not None else 'unknown'} "
            f"disk_emergency={bool(payload.get('disk_emergency_active'))} "
            f"sqlite_write_wait={_perf_duration_text(payload.get('sqlite_write_wait_ms') or 0)} "
            f"sqlite_query={_perf_duration_text(payload.get('sqlite_query_latency_ms') or 0)} "
            f"event_loop_lag={_perf_duration_text(payload.get('tool_pressure_event_loop_lag_ms') or 0)}"
        )
        lines.append(
            f"Tiers: budget={payload.get('budget_state')} machine={payload.get('machine_pressure_state')} "
            f"local={payload.get('local_pressure_state')} throttled_since={payload.get('tool_pressure_throttled_since') or '-'} "
            f"critical_since={payload.get('tool_pressure_critical_since') or '-'} sample_at={payload.get('pressure_sample_at') or '-'}"
        )
        lines.append('')
        lines.append(self._render_perf_window_report(rows=rows, window_minutes=window_minutes))
        return '\n'.join(lines)

    def _perf_coverage_facts(self) -> dict[str, Any]:
        """"为什么没有行"只给可观测事实，不断言唯一原因（原因要靠 worker 日志判）。"""
        total = int(self.store.count_perf_samples())
        newest = self.store.newest_perf_sample() or {}
        newest_stamp = self._perf_sample_time(newest.get('sampled_at'))
        newest_age_seconds: float | None = None
        if newest_stamp is not None:
            newest_age_seconds = max(0.0, (datetime.now(timezone.utc) - newest_stamp).total_seconds())
        live = self.worker_status_payload()
        heartbeat_age_ms = _perf_number(live.get('worker_heartbeat_age_ms'))
        return {
            'rows_total': total,
            'newest_at': str(newest.get('sampled_at') or ''),
            'newest_age_seconds': newest_age_seconds,
            'worker_id': str(newest.get('worker_id') or ''),
            'worker_state': str(live.get('worker_state') or 'unknown'),
            'worker_online': bool(live.get('worker_online')),
            'heartbeat_age_seconds': None if heartbeat_age_ms is None else round(heartbeat_age_ms / 1000.0, 1),
        }

    def _perf_coverage_lines(self) -> list[str]:
        facts = self._perf_coverage_facts()
        total = int(facts['rows_total'])
        heartbeat_text = (
            f"worker state={facts['worker_state']}"
            + (f" heartbeat_age={facts['heartbeat_age_seconds']}s" if facts['heartbeat_age_seconds'] is not None else '')
        )
        if total <= 0:
            lines = [f'Perf table: rows_total=0; {heartbeat_text}.']
            if facts['heartbeat_age_seconds'] is None:
                lines.append(
                    'There is no worker heartbeat row at all - the sampler runs inside that heartbeat, so nothing '
                    'can be recorded until a worker starts.'
                )
            elif facts['worker_online']:
                lines.append(
                    'The worker is heartbeating yet no perf row has ever landed, so the gap is on the write side: '
                    'that worker process runs a build without the sampler, or its write fails. Discriminator in the '
                    "worker log: 'worker heartbeat beat failed' / 'failed to record perf sample'."
                )
            else:
                lines.append('The worker heartbeat is stale/offline - sampling stops with it.')
            lines.append('An interval with no rows cannot be reconstructed afterwards.')
            return lines
        lines = [f'Perf table: rows_total={total}, newest sample={facts["newest_at"]} (worker_id={facts["worker_id"] or "-"})']
        newest_age = facts['newest_age_seconds']
        if newest_age is not None and newest_age > 90.0:
            lines.append(
                f'Sampling stopped {_perf_seconds_text(newest_age)} ago while {heartbeat_text} - the process that '
                'sampled has since been replaced, or the heartbeat beat no longer reaches the sampler (same two '
                'worker-log lines decide).'
            )
        lines.append('An interval with no rows cannot be reconstructed afterwards.')
        return lines

    def _perf_coverage_brief(self) -> str:
        """失速事件用的短版覆盖说明：只回答"为什么没有样本"，不铺开成多行。"""
        facts = self._perf_coverage_facts()
        total = int(facts['rows_total'])
        heartbeat = (
            f'worker heartbeat age={facts["heartbeat_age_seconds"]}s'
            if facts['heartbeat_age_seconds'] is not None
            else f"worker state={facts['worker_state']}"
        )
        if total <= 0:
            if facts['heartbeat_age_seconds'] is None:
                return 'no perf rows and no worker heartbeat row - coverage gap, not evidence of no pressure'
            return f'no perf rows ever written ({heartbeat}) - a coverage gap, not evidence of no pressure'
        newest_age = facts['newest_age_seconds']
        if newest_age is not None and newest_age > 90.0:
            return (
                f'perf sampling stopped {_perf_seconds_text(newest_age)} ago, {heartbeat} '
                '- a coverage gap, not evidence of no pressure'
            )
        return f'no perf samples in this window (rows_total={total}) - a coverage gap, not evidence of no pressure'

    def _perf_window_brief(self, *, start_at: datetime, end_at: datetime) -> str:
        """失速窗口的一行性能判读，与 perf_inspect 共用同一份聚合口径。"""
        rows = self._perf_sample_rows(start_at=start_at, end_at=end_at)
        if not rows:
            return f'perf: {self._perf_coverage_brief()}'
        stats = self._perf_sample_stats(rows)
        axes = stats['axes']
        parts = [f"perf samples={stats['sample_count']}", _perf_tiers_text(stats['tier_seconds'])]
        for label in ('tq_wait', 'tq_oldest', 'nq_wait', 'cpu', 'mem', 'disk', 'db_wait', 'lag'):
            axis = axes.get(label)
            if not axis:
                continue
            kind = _PERF_AXIS_KIND[label]
            if kind == 'percent':
                parts.append(f'{label} max={axis["max"]:.0f}%')
            elif kind == 'ms':
                parts.append(f'{label} max={_perf_duration_text(axis["max"])}')
            elif kind == 'bytes':
                parts.append(f'{label} min={_perf_bytes_text(axis["min"])}')
            else:
                parts.append(f'{label} max={axis["max"]:.0f}')
        if stats['gaps']:
            parts.append(f'sampling gaps={len(stats["gaps"])} (largest {_perf_seconds_text(max(gap["seconds"] for gap in stats["gaps"]))})')
        if float(stats['disk_emergency_seconds']) > 0.0:
            parts.append(f'disk_emergency={_perf_seconds_text(stats["disk_emergency_seconds"])}')
        if float(stats['restricted_seconds']) <= 0.0 and not stats['gaps']:
            parts.append('no resource restriction in this window, so the silence was not perf-caused')
        return '; '.join(parts)

    def _clamp_depth(self, requested: int | None) -> int:
        if requested is None:
            return self._default_max_depth
        return max(0, min(int(requested), self._hard_max_depth))


def _runtime_task_default_max_depth(runtime: dict[str, Any] | None) -> int | None:
    payload = runtime if isinstance(runtime, dict) else {}
    task_defaults = payload.get('task_defaults')
    if not isinstance(task_defaults, dict):
        return None
    raw_depth = task_defaults.get('max_depth', task_defaults.get('maxDepth'))
    if raw_depth in (None, ''):
        return None
    try:
        return int(raw_depth)
    except (TypeError, ValueError):
        return None


def _tool_runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


_TASK_KEYWORDS_PARAM = '\u4efb\u52a1\u5173\u952e\u8bcd'
_TASK_ID_LIST_PARAM = '\u4efb\u52a1id\u5217\u8868'
_TASK_SCOPE_PARAM = '查询范围'
_TASK_SCOPE_GLOBAL = '全局'


def _tool_session_scope(runtime: dict[str, Any], kwargs: dict[str, Any]) -> str | None:
    """按「查询范围」参数解析会话口径：全局=None（跨会话全量），缺省=当前会话。"""
    scope = str(kwargs.get(_TASK_SCOPE_PARAM) or '').strip()
    if scope == _TASK_SCOPE_GLOBAL:
        return None
    return str(runtime.get('session_key') or 'web:shared')


class TaskSummaryTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_summary'

    @property
    def description(self) -> str:
        return '返回总任务、进行中（含其中暂停数）、失败任务数量；默认统计当前会话，查询范围=全局时统计所有会话。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                _TASK_SCOPE_PARAM: {
                    'type': 'string',
                    'enum': ['本会话', '全局'],
                    'description': '本会话（默认）=只统计当前会话创建的任务；全局=统计所有会话的任务。',
                },
            },
        }

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        return self._service.summary(_tool_session_scope(runtime, kwargs))


class GetTasksTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_list'

    @property
    def description(self) -> str:
        return '按任务类型返回任务 id 列表和简要描述；默认当前会话，查询范围=全局时跨会话（全局结果逐行附状态与会话归属）。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务类型': {'type': 'integer', 'enum': [1, 2, 3, 4], 'description': '1=所有任务，2=进行中任务，3=失败任务，4=未读任务。'},
                _TASK_SCOPE_PARAM: {
                    'type': 'string',
                    'enum': ['本会话', '全局'],
                    'description': '本会话（默认）=只列当前会话的任务；全局=列所有会话的任务。',
                },
            },
            'required': ['任务类型'],
        }

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        task_type = int(kwargs.get('任务类型'))
        return self._service.get_tasks(_tool_session_scope(runtime, kwargs), task_type)


class TaskStatsTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_stats'

    @property
    def description(self) -> str:
        return 'List task statistics globally or by task id, including prompt preview and task disk usage.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'mode': {
                    'type': 'string',
                    'enum': ['list', 'id'],
                    'description': 'list lists tasks by date range; id returns specific task ids.',
                },
                _TASK_KEYWORDS_PARAM: {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Optional keyword list for list mode. Matches the initial user_request with OR semantics.',
                },
                'from': {
                    'type': 'string',
                    'description': 'Required in list mode. Date format: YYYY/M/D.',
                },
                'to': {
                    'type': 'string',
                    'description': 'Required in list mode. Date format: YYYY/M/D.',
                },
                _TASK_ID_LIST_PARAM: {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Required in id mode. Task ids to inspect in input order.',
                },
            },
            'required': ['mode'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        normalized_mode = str((params or {}).get('mode') or '').strip().lower()
        if normalized_mode == 'list':
            if not str((params or {}).get('from') or '').strip():
                errors.append('from is required when mode=list')
            if not str((params or {}).get('to') or '').strip():
                errors.append('to is required when mode=list')
        if normalized_mode == 'id' and not list((params or {}).get(_TASK_ID_LIST_PARAM) or []):
            errors.append(f'{_TASK_ID_LIST_PARAM} is required when mode=id')
        return errors

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        result = self._service.task_stats(
            mode=str(kwargs.get('mode') or '').strip(),
            task_keywords=kwargs.get(_TASK_KEYWORDS_PARAM),
            date_from=str(kwargs.get('from') or '').strip(),
            date_to=str(kwargs.get('to') or '').strip(),
            task_ids=kwargs.get(_TASK_ID_LIST_PARAM),
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class TaskDeleteTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_delete'

    @property
    def description(self) -> str:
        return 'Preview or confirm deletion of one or more saved tasks.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'mode': {
                    'type': 'string',
                    'enum': ['preview', 'confirm'],
                    'description': 'preview returns candidate tasks and a confirmation token; confirm performs deletion.',
                },
                _TASK_ID_LIST_PARAM: {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Task ids to delete. Supports batch operations.',
                },
                'confirmation_token': {
                    'type': 'string',
                    'description': 'Required in confirm mode. Must come from a prior preview of the same task id batch.',
                },
            },
            'required': ['mode', _TASK_ID_LIST_PARAM],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        normalized_mode = str((params or {}).get('mode') or '').strip().lower()
        if normalized_mode == 'confirm' and not str((params or {}).get('confirmation_token') or '').strip():
            errors.append('confirmation_token is required when mode=confirm')
        return errors

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        normalized_mode = str(kwargs.get('mode') or '').strip().lower()
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        if normalized_mode == 'preview':
            result = self._service.task_delete_preview(
                task_ids=kwargs.get(_TASK_ID_LIST_PARAM),
                session_id=session_id,
            )
        else:
            result = self._service.task_delete_confirm(
                task_ids=kwargs.get(_TASK_ID_LIST_PARAM),
                confirmation_token=str(kwargs.get('confirmation_token') or '').strip(),
                session_id=session_id,
            )
            if inspect.isawaitable(result):
                result = await result
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class TaskFailedNodesTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_failed_nodes'

    @property
    def description(self) -> str:
        return '按任务 id 返回当前任务树中的失败节点 id 列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {'任务id': {'type': 'string', 'description': '目标任务 id。'}},
            'required': ['任务id'],
        }

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        return self._service.failed_node_ids(task_id)


class _LegacyTaskNodeDetailToolMojibakeB(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '_legacy_task_node_detail_removed_b0'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务id': {'type': 'string', 'description': '目标任务 id。'},
                '节点id': {'type': 'string', 'description': '目标节点 id。'},
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        return self._service.node_detail(task_id, node_id)


class _LegacyTaskNodeDetailToolMojibakeA(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '_legacy_task_node_detail_removed_b'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '浠诲姟id': {'type': 'string', 'description': '目标任务 id。'},
                '鑺傜偣id': {'type': 'string', 'description': '目标节点 id。'},
                'detail_level': build_detail_level_schema(
                    description='summary 返回轻量节点详情与 refs；full 返回完整执行轨迹和完整工件列表。',
                ),
            },
            'required': ['浠诲姟id', '鑺傜偣id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('浠诲姟id') or '').strip()
        node_id = str(kwargs.get('鑺傜偣id') or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            result = self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            result = self._service.node_detail(task_id, node_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class _TaskNodeDetailToolMojibakeCurrent(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '_legacy_task_node_detail_removed_a'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务id': {'type': 'string', 'description': '目标任务 id。'},
                '节点id': {'type': 'string', 'description': '目标节点 id。'},
                'detail_level': build_detail_level_schema(
                    description='summary 返回轻量节点详情与 refs/工件预览；full 返回完整执行轨迹和完整工件列表。',
                ),
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            return self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            return self._service.node_detail(task_id, node_id)


class TaskNodeDetailTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_node_detail'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务id': {'type': 'string', 'description': '目标任务 id。'},
                '节点id': {'type': 'string', 'description': '目标节点 id。'},
                'detail_level': build_detail_level_schema(
                    description='summary 返回轻量节点详情与 refs/工件预览；full 返回完整执行轨迹和完整工件列表。',
                ),
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            result = self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            result = self._service.node_detail(task_id, node_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


