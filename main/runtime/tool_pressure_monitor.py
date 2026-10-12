from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Sequence

from main.storage.disk_guard import (
    disk_policies,
    disk_waterline_snapshot,
    emergency_threshold_bytes,
)

try:  # pragma: no cover - optional dependency in local dev before reinstall
    import psutil
except Exception:  # pragma: no cover - handled by runtime fallback
    psutil = None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


# 回合闸的余量目标算法常量。压力轴的阈值一律复用上面 observe 用的那一份 warn/safe 线，
# 只有这几组是新的，且都按实盘标定：
# - 轴比上下限：安静时一拍最多翻倍，最挤时一拍最多收到 1/4（实测 lag 见过 12.0 s、
#   一次 SQLite 读 141 s，靠变化率而不是靠跳幅兜底）。
_ENTRY_AXIS_FLOOR_RATIO = 0.25
_ENTRY_AXIS_CEILING_RATIO = 2.0
# - 上游限流惩罚不再参与回合闸的容量判断（操作者 2026-10-12 定）：限流恶化由模型链的
#   退避/换槽车道收口。历史判据曾是"越过 p90 档只保持、越过 p99 档(4.0)才缓减"，实测
#   penalty_429 p50=0 / p90=0.18 / p99=4.16 / max=13.57（6255 条路由观测）；10-12 那天
#   下午这段全程 12.5–23.3，把闸钉在地板 40 多分钟、execution 排队 15–30 个节点，
#   而同期完成量 22–36 次/分——它把"该不该给格子"和"上游吃不吃得下"混成了同一个判断。
# - 共享池：等待驱动的扩容触发线与硬顶。满载且有等待连续超过这么多秒就先放人，
#   收益由完成量基准在下一拍复核；上限按地板倍数封顶，因为"内存轴放行"并不等于
#   值得放（10-12 一格 14.6 MB、可用 1.8 GB ⇒ 内存轴自己会放行到 100+ 格）。
_ENTRY_WAIT_GROWTH_SECONDS = 120.0
_ENTRY_POOL_MAX_FACTOR = 1.5
_ENTRY_POOL_KEY = 'pool'
# - 回环连接轴：本进程挂在自家 web 端口上的 ESTABLISHED 条数。标定依据是同一天两次
#   实测——07:21 重启后 35 分钟攒到 527、2 小时 3 846（≈1470 条/小时，两侧都不关），
#   而 23:26 那份实例按同样斜率跑到 ~5 000 时机器开始报 WinError 10055（套接字缓冲耗尽，
#   表现是网页打不开 + 模型调用从 363 次/小时掉到 32）。warn 取崩溃点的四成。
_ENTRY_LOOPBACK_WARN_CONNECTIONS = 2000.0
_ENTRY_LOOPBACK_CRITICAL_CONNECTIONS = 4000.0
# - 单格内存成本的兜底值：实测 9 格在飞时 worker RSS 1.04 GB，凌晨 27 格时 Private 2.4 GB
#   （≈89–115 MB/格）。有回归样本时用回归值，没有时用这一档。
_ENTRY_SLOT_MEMORY_FALLBACK_BYTES = 110 * 1024 * 1024
_ENTRY_SLOT_MEMORY_MIN_BYTES = 8 * 1024 * 1024
_ENTRY_SLOT_MEMORY_MAX_BYTES = 2 * 1024 * 1024 * 1024
_ENTRY_MEMORY_SAMPLE_WINDOW_SECONDS = 600.0
_ENTRY_MEMORY_MIN_SPAN_SLOTS = 2
_ENTRY_MEMORY_MIN_DELTA_BYTES = 64 * 1024 * 1024
_ENTRY_MEMORY_MIN_AVG_SLOTS = 4
_ENTRY_MEMORY_ESTIMATE_WINDOW_SECONDS = 600.0
# - 节拍：一次移动之后至少隔一个反馈周期再动。21:19 重启后按拍(1s)动时，38 格→1 格
#   只用了一次性能采样窗口（21:40→21:41），lag 常态 1.1–7.9 s，闸位在 1↔38 之间抖振——
#   那是塌方不是控制。一拍 = 1 s，故 45 s ≈ 一个实测调用时长（p50 37 s）加余量。
_ENTRY_BEAT_SECONDS = 45.0
_ENTRY_GROWTH_SLOTS_PER_BEAT = 2
_ENTRY_SHRINK_SLOTS_PER_BEAT = 1
# - 抬闸必须换来吞吐：实盘把在飞从 8 抬到 46，完成频率只从 6.93 涨到 7.23 次/分（+4%），
#   单次调用 p50 却从 38.6 s 涨到 138 s。所以"闸卡住需求"不能当增长判据（饱和时它恒真），
#   改用它替代：这一拍比上一次抬闸时至少多这么多"每分钟发起的模型请求"（0.5 ≈ 基线 7/min 的 7%）。
_ENTRY_MIN_THROUGHPUT_GAIN = 0.5
_ENTRY_THROUGHPUT_WINDOW_SECONDS = 90.0
# 吞吐基准不能永久钉住：真实负载会自己降下来（树快跑完了）。过了这个时长就再放行一次
# 试探性抬格，间隔取 4 个节拍，比抖振（此前 45 s 一次来回）慢一个量级。
_ENTRY_PROBE_AFTER_SECONDS = 180.0


class _EventLoopLagSampler:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.RLock()
        self._pending_sent_at = 0.0
        self._last_lag_ms = 0.0

    def ping(self) -> None:
        with self._lock:
            if self._pending_sent_at > 0.0:
                return
            sent_at = time.perf_counter()
            self._pending_sent_at = sent_at
        try:
            self._loop.call_soon_threadsafe(self._resolve, sent_at)
        except RuntimeError:
            with self._lock:
                self._pending_sent_at = 0.0

    def sample(self, now_mono: float | None = None) -> float:
        current = float(now_mono if now_mono is not None else time.perf_counter())
        with self._lock:
            if self._pending_sent_at > 0.0:
                return max(0.0, (current - self._pending_sent_at) * 1000.0)
            return max(0.0, float(self._last_lag_ms or 0.0))

    def _resolve(self, sent_at: float) -> None:
        current = time.perf_counter()
        with self._lock:
            if self._pending_sent_at <= 0.0:
                return
            self._last_lag_ms = max(0.0, (current - self._pending_sent_at) * 1000.0)
            self._pending_sent_at = 0.0


class WorkerPressureMonitor:
    def __init__(
        self,
        *,
        controller,
        store,
        sample_seconds: float = 1.0,
        recover_window_seconds: float = 1.0,
        warn_consecutive_samples: int = 3,
        safe_consecutive_samples: int = 3,
        pressure_snapshot_stale_after_seconds: float = 3.0,
        event_loop_warn_ms: float = 250.0,
        event_loop_safe_ms: float = 100.0,
        event_loop_critical_ms: float = 1500.0,
        writer_queue_warn: int = 50,
        writer_queue_safe: int = 10,
        writer_queue_critical: int = 100,
        sqlite_write_wait_warn_ms: float = 200.0,
        sqlite_write_wait_safe_ms: float = 50.0,
        sqlite_write_wait_critical_ms: float = 250.0,
        sqlite_query_warn_ms: float = 150.0,
        sqlite_query_safe_ms: float = 30.0,
        sqlite_query_critical_ms: float = 250.0,
        machine_cpu_warn_percent: float = 85.0,
        machine_cpu_safe_percent: float = 55.0,
        machine_cpu_critical_percent: float = 95.0,
        machine_memory_warn_percent: float = 88.0,
        machine_memory_safe_percent: float = 75.0,
        machine_memory_critical_percent: float = 94.0,
        machine_disk_busy_warn_percent: float = 70.0,
        machine_disk_busy_safe_percent: float = 35.0,
        machine_disk_busy_critical_percent: float = 90.0,
        process_cpu_warn_ratio: float = 0.85,
        process_cpu_safe_ratio: float = 0.50,
        max_pressure_dwell_seconds: float = 60.0,
        max_tool_wait_ms: float = 30000.0,
        local_recovery_enabled: bool = True,
        system_metrics_sampler: Callable[[], dict[str, Any]] | None = None,
        disk_watermark_paths: Sequence[str] | None = None,
        rate_limit_observer: Callable[[], dict[str, Any]] | None = None,
        loop_observer: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._controller = controller
        self._store = store
        self._system_metrics_sampler = system_metrics_sampler
        # 上游限流观测（回合闸的第四条轴）：由 runtime_service 注入模型负载均衡器的
        # 聚合读数，取不到就当没有这条约束。回合闸不许为了它去新起一套 429 计数。
        self._rate_limit_observer = rate_limit_observer
        # 回环连接观测（第五根积压轴）：读 runtime_service 现数的 ESTABLISHED 条数与
        # 事件队列计数，取不到就当没有这条约束。
        self._loop_observer = loop_observer
        self._process_handle: Any = None
        self._entry_memory_samples: deque[tuple[float, int, int]] = deque()
        self._entry_slot_memory_estimates: deque[tuple[float, int]] = deque()
        self._entry_slot_memory_bytes = _ENTRY_SLOT_MEMORY_FALLBACK_BYTES
        self._entry_idle_memory_bytes = 0
        self._entry_last_step_at = 0.0
        self._entry_rpm_samples: deque[tuple[float, float]] = deque()
        # 决策用的窗口是"完成量"（换来了回答的那几发），不是"发起量"。
        # `_entry_rpm_at_last_growth` 已退役：它把 429 重试算成吞吐，10-12 实测读数
        # 是完成量的 3–4 倍，"抬格换来了吞吐"在这条轴上恒成立。
        self._entry_completion_samples: deque[tuple[float, float]] = deque()
        self._entry_completion_bar_at_last_growth = None
        self._entry_binding_since_mono = 0.0
        self._entry_rpm_bar_set_at = 0.0
        # 磁盘治理（P1）：水位探测路径（工作区/存储目录，通常同盘）与紧急态状态。
        # 紧急态是 monitor 侧独立布尔镜像（controller 侧另有硬闸字段）——不进
        # pressure_state 白名单，避免被 dwell/starvation 逃逸阀与 idle reset 冲掉。
        self._disk_watermark_paths: tuple[str, ...] = tuple(str(p) for p in (disk_watermark_paths or ()) if str(p or '').strip())
        self._disk_emergency_active = False
        self._disk_emergency_since = ''
        self._disk_emergency_streak = 0
        self._disk_recovery_streak = 0
        self._disk_emergency_enter_hook: Callable[[], None] | None = None
        self._disk_emergency_exit_hook: Callable[[], None] | None = None
        self._lock = threading.RLock()
        self._sample_seconds = max(0.1, float(sample_seconds or 1.0))
        self._recover_window_seconds = max(0.1, float(recover_window_seconds or 1.0))
        self._warn_consecutive_samples = max(1, int(warn_consecutive_samples or 1))
        self._safe_consecutive_samples = max(1, int(safe_consecutive_samples or 1))
        self._pressure_snapshot_stale_after_seconds = max(0.1, float(pressure_snapshot_stale_after_seconds or 3.0))
        self._event_loop_warn_ms = max(0.0, float(event_loop_warn_ms or 0.0))
        self._event_loop_safe_ms = max(0.0, float(event_loop_safe_ms or 0.0))
        self._event_loop_critical_ms = max(0.0, float(event_loop_critical_ms or 0.0))
        self._writer_queue_warn = max(0, int(writer_queue_warn or 0))
        self._writer_queue_safe = max(0, int(writer_queue_safe or 0))
        self._writer_queue_critical = max(1, int(writer_queue_critical or 1))
        self._sqlite_write_wait_warn_ms = max(0.0, float(sqlite_write_wait_warn_ms or 0.0))
        self._sqlite_write_wait_safe_ms = max(0.0, float(sqlite_write_wait_safe_ms or 0.0))
        self._sqlite_write_wait_critical_ms = max(0.0, float(sqlite_write_wait_critical_ms or 0.0))
        self._sqlite_query_warn_ms = max(0.0, float(sqlite_query_warn_ms or 0.0))
        self._sqlite_query_safe_ms = max(0.0, float(sqlite_query_safe_ms or 0.0))
        self._sqlite_query_critical_ms = max(0.0, float(sqlite_query_critical_ms or 0.0))
        self._machine_cpu_warn_percent = max(0.0, float(machine_cpu_warn_percent or 0.0))
        self._machine_cpu_safe_percent = max(0.0, float(machine_cpu_safe_percent or 0.0))
        self._machine_cpu_critical_percent = max(0.0, float(machine_cpu_critical_percent or 0.0))
        self._machine_memory_warn_percent = max(0.0, float(machine_memory_warn_percent or 0.0))
        self._machine_memory_safe_percent = max(0.0, float(machine_memory_safe_percent or 0.0))
        self._machine_memory_critical_percent = max(0.0, float(machine_memory_critical_percent or 0.0))
        self._machine_disk_busy_warn_percent = max(0.0, float(machine_disk_busy_warn_percent or 0.0))
        self._machine_disk_busy_safe_percent = max(0.0, float(machine_disk_busy_safe_percent or 0.0))
        self._machine_disk_busy_critical_percent = max(0.0, float(machine_disk_busy_critical_percent or 0.0))
        self._process_cpu_warn_ratio = max(0.0, float(process_cpu_warn_ratio or 0.0))
        self._process_cpu_safe_ratio = max(0.0, float(process_cpu_safe_ratio or 0.0))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lag_sampler: _EventLoopLagSampler | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._consecutive_warn = 0
        self._consecutive_safe = 0
        self._consecutive_machine_warn = 0
        self._consecutive_machine_safe = 0
        self._consecutive_local_critical = 0
        self._consecutive_local_safe = 0
        self._restricted_since_mono = 0.0
        self._max_pressure_dwell_seconds = max(0.0, float(max_pressure_dwell_seconds or 0.0))
        self._max_tool_wait_ms = max(0.0, float(max_tool_wait_ms or 0.0))
        self._local_recovery_enabled = bool(local_recovery_enabled)
        self._last_waiting_count = 0
        self._last_recovery_step_at = 0.0
        self._sample_mono = 0.0
        self._last_disk_sample: Any = None
        self._last_perdisk_sample: dict[str, Any] | None = None
        self._last_disk_sample_mono = 0.0
        self._snapshot: dict[str, Any] = {
            'machine_pressure_available': False,
            'machine_pressure_cpu_percent': 0.0,
            'machine_pressure_memory_percent': 0.0,
            'machine_pressure_disk_busy_percent': 0.0,
            'machine_pressure_disk_busy_available': False,
            'machine_pressure_disk_read_bytes_per_sec': 0.0,
            'machine_pressure_disk_write_bytes_per_sec': 0.0,
            'machine_disk_free_bytes': -1,
            'machine_disk_total_bytes': -1,
            'machine_disk_usage_percent': 0.0,
            'disk_emergency_active': False,
            'disk_emergency_since': '',
            'tool_pressure_event_loop_lag_ms': 0.0,
            'tool_pressure_writer_queue_depth': 0,
            'tool_pressure_process_cpu_ratio': 0.0,
            'sqlite_write_wait_ms': 0.0,
            'sqlite_query_latency_ms': 0.0,
            'machine_pressure_state': 'unknown',
            'local_pressure_state': 'unknown',
            'budget_state': 'normal',
            'pressure_sample_at': '',
            'tool_pressure_sample_at': '',
            'tool_pressure_self_heal_reason': '',
            # 回合闸的余量目标与它的输入读数：闸位为什么动，必须能在历史里读出来，
            # 否则操作员只能看到"节点数没变"而看不到是哪条轴拦的。
            'entry_gate_targets': {},
            'entry_gate_limits': {},
            'entry_gate_pool': {},
            'entry_gate_reserved': {},
            'entry_gate_running_total': 0,
            'entry_slot_memory_bytes': int(_ENTRY_SLOT_MEMORY_FALLBACK_BYTES),
            'entry_idle_memory_bytes': 0,
            'entry_throughput_rpm_60s': 0.0,
            'entry_completions_60s': 0.0,
            'entry_completions_at_last_growth': None,
            'entry_binding_seconds': 0.0,
            'entry_seconds_since_step': 0.0,
            'entry_loopback_established': 0,
            'entry_loopback_pool_total': -1,
            'entry_loopback_pool_leased': -1,
            'entry_event_queue_depth': 0,
            'entry_event_dropped_total': 0,
            'entry_event_batch_request_total': 0,
            'entry_event_batch_item_total': 0,
            'entry_event_single_fallback_total': 0,
            'entry_event_coalesced_total': 0,
            'entry_event_dropped_coalesced_total': 0,
            'entry_event_dropped_other_total': 0,
            'entry_event_queue_bytes_estimated': 0,
            'entry_callback_error_total': 0,
            'worker_memory_bytes': -1,
            'machine_memory_total_bytes': -1,
            'machine_memory_available_bytes': -1,
            'model_rate_penalty_429_max': 0.0,
            'model_rolling_rpm_60s_max': 0.0,
            'model_rolling_rpm_60s_sum': 0,
        }

    def set_disk_emergency_hooks(
        self,
        *,
        enter: Callable[[], None] | None = None,
        exit_: Callable[[], None] | None = None,
    ) -> None:
        """注入磁盘紧急态边沿回调（线程安全、必须立即返回——在 1s 采样线程内调用）。

        enter：跌破紧急线（连续 N 样本确认）——runtime_service 用它调度全任务自动暂停+告警；
        exit_：回到紧急线之上（连续 N 样本确认）——只清告警，任务保持 paused。
        """
        with self._lock:
            if enter is not None:
                self._disk_emergency_enter_hook = enter
            if exit_ is not None:
                self._disk_emergency_exit_hook = exit_

    def set_disk_watermark_paths(self, paths: Sequence[str]) -> None:
        with self._lock:
            self._disk_watermark_paths = tuple(str(p) for p in (paths or ()) if str(p or '').strip())

    def configure(
        self,
        *,
        sample_seconds: float,
        recover_window_seconds: float,
        warn_consecutive_samples: int,
        safe_consecutive_samples: int,
        pressure_snapshot_stale_after_seconds: float,
        event_loop_warn_ms: float,
        event_loop_safe_ms: float,
        event_loop_critical_ms: float,
        writer_queue_warn: int,
        writer_queue_safe: int,
        writer_queue_critical: int,
        sqlite_write_wait_warn_ms: float,
        sqlite_write_wait_safe_ms: float,
        sqlite_write_wait_critical_ms: float,
        sqlite_query_warn_ms: float,
        sqlite_query_safe_ms: float,
        sqlite_query_critical_ms: float,
        machine_cpu_warn_percent: float,
        machine_cpu_safe_percent: float,
        machine_cpu_critical_percent: float,
        machine_memory_warn_percent: float,
        machine_memory_safe_percent: float,
        machine_memory_critical_percent: float,
        machine_disk_busy_warn_percent: float,
        machine_disk_busy_safe_percent: float,
        machine_disk_busy_critical_percent: float,
        process_cpu_warn_ratio: float,
        process_cpu_safe_ratio: float,
        max_pressure_dwell_seconds: float = 60.0,
        max_tool_wait_ms: float = 30000.0,
        local_recovery_enabled: bool = True,
    ) -> None:
        with self._lock:
            self._sample_seconds = max(0.1, float(sample_seconds or 1.0))
            self._recover_window_seconds = max(0.1, float(recover_window_seconds or 1.0))
            self._warn_consecutive_samples = max(1, int(warn_consecutive_samples or 1))
            self._safe_consecutive_samples = max(1, int(safe_consecutive_samples or 1))
            self._pressure_snapshot_stale_after_seconds = max(0.1, float(pressure_snapshot_stale_after_seconds or 3.0))
            self._event_loop_warn_ms = max(0.0, float(event_loop_warn_ms or 0.0))
            self._event_loop_safe_ms = max(0.0, float(event_loop_safe_ms or 0.0))
            self._event_loop_critical_ms = max(0.0, float(event_loop_critical_ms or 0.0))
            self._writer_queue_warn = max(0, int(writer_queue_warn or 0))
            self._writer_queue_safe = max(0, int(writer_queue_safe or 0))
            self._writer_queue_critical = max(1, int(writer_queue_critical or 1))
            self._sqlite_write_wait_warn_ms = max(0.0, float(sqlite_write_wait_warn_ms or 0.0))
            self._sqlite_write_wait_safe_ms = max(0.0, float(sqlite_write_wait_safe_ms or 0.0))
            self._sqlite_write_wait_critical_ms = max(0.0, float(sqlite_write_wait_critical_ms or 0.0))
            self._sqlite_query_warn_ms = max(0.0, float(sqlite_query_warn_ms or 0.0))
            self._sqlite_query_safe_ms = max(0.0, float(sqlite_query_safe_ms or 0.0))
            self._sqlite_query_critical_ms = max(0.0, float(sqlite_query_critical_ms or 0.0))
            self._machine_cpu_warn_percent = max(0.0, float(machine_cpu_warn_percent or 0.0))
            self._machine_cpu_safe_percent = max(0.0, float(machine_cpu_safe_percent or 0.0))
            self._machine_cpu_critical_percent = max(0.0, float(machine_cpu_critical_percent or 0.0))
            self._machine_memory_warn_percent = max(0.0, float(machine_memory_warn_percent or 0.0))
            self._machine_memory_safe_percent = max(0.0, float(machine_memory_safe_percent or 0.0))
            self._machine_memory_critical_percent = max(0.0, float(machine_memory_critical_percent or 0.0))
            self._machine_disk_busy_warn_percent = max(0.0, float(machine_disk_busy_warn_percent or 0.0))
            self._machine_disk_busy_safe_percent = max(0.0, float(machine_disk_busy_safe_percent or 0.0))
            self._machine_disk_busy_critical_percent = max(0.0, float(machine_disk_busy_critical_percent or 0.0))
            self._process_cpu_warn_ratio = max(0.0, float(process_cpu_warn_ratio or 0.0))
            self._process_cpu_safe_ratio = max(0.0, float(process_cpu_safe_ratio or 0.0))
            self._max_pressure_dwell_seconds = max(0.0, float(max_pressure_dwell_seconds or 0.0))
            self._max_tool_wait_ms = max(0.0, float(max_tool_wait_ms or 0.0))
            self._local_recovery_enabled = bool(local_recovery_enabled)

    def snapshot(self) -> dict[str, Any]:
        current_mono = time.perf_counter()
        with self._lock:
            payload = dict(self._snapshot)
            sample_mono = float(self._sample_mono or 0.0)
            stale_after_ms = self._pressure_snapshot_stale_after_seconds * 1000.0
            sample_age_ms = max(0.0, (current_mono - sample_mono) * 1000.0) if sample_mono > 0.0 else float('inf')
            sample_fresh = (
                sample_mono > 0.0
                and bool(payload.get('machine_pressure_available'))
                and sample_age_ms <= stale_after_ms
            )
        payload['pressure_sample_age_ms'] = round(sample_age_ms, 3) if sample_mono > 0.0 else None
        payload['pressure_snapshot_fresh'] = bool(sample_fresh)
        payload.update(self._controller.snapshot())
        # 闸口排队与容量：面板与 perf_samples 要标量才能当轴用。缺这两个数时，闸贴顶排队
        # 185 个节点的窗口在历史里读不出来（节点侧的 `node_queue_waiting_count` 量的是
        # 过闸之后等模型 permit 的队列，闸收紧时它恒 0）。
        # 容量是**共享池**：`entry_gate_limits_total` 直接取池位，不再把各角色的读数相加
        # （角色 dict 里的 limit 现在是池位回显，相加会变成两倍）。
        queued_by_role = dict(payload.get('entry_gate_queued') or {})
        pool_view = dict(payload.get('entry_gate_pool') or {})
        limits_by_role = dict(payload.get('entry_gate_limit') or payload.get('entry_gate_limits') or {})
        payload['entry_gate_queued_total'] = (
            int(pool_view.get('queued') or 0)
            if pool_view
            else sum(int(value or 0) for value in queued_by_role.values())
        )
        pool_limit = int(pool_view.get('limit') or 0) if pool_view else 0
        payload['entry_gate_limits_total'] = (
            pool_limit if pool_limit > 0 else max(1, sum(int(value or 0) for value in limits_by_role.values()))
        )
        return payload

    @staticmethod
    def _entry_axis_ratio(warn_value: float, measured: float) -> float:
        """一根积压轴的余量比：warn 线一半以下封顶 2.0（一拍最多翻倍），贴到 warn 线是
        1.0（保持当前），越过 warn 线开始收，最低 0.25。阈值直接复用压力状态用的那一份
        warn 线，不为回合闸另起一套数字。"""
        warn = max(0.0, float(warn_value or 0.0))
        if warn <= 0.0:
            return _ENTRY_AXIS_CEILING_RATIO
        value = max(0.0, float(measured or 0.0))
        ratio = warn / max(value, warn * 0.5)
        return min(_ENTRY_AXIS_CEILING_RATIO, max(_ENTRY_AXIS_FLOOR_RATIO, ratio))

    def _update_entry_slot_memory(
        self,
        *,
        current_mono: float,
        running_slots: int,
        worker_memory_bytes: Any,
    ) -> None:
        """估一格内存成本，取窗口内各估计的最大值。

        两种估计：(a) 滑动窗口里 (在飞格数, RSS) 的跨度回归；(b) 相对"空载 RSS 基线"
        （running 归零时见过的最小 RSS）的平均成本 (rss - idle) / running。平坦期跨度
        回归会失真——实盘 21:46 起 running 一直 46–48、RSS 稳定在 2.0–2.1 GB，(a) 只算出
        9–29 MB/格，于是内存轴反过来抬高了上限（真实边际 ≈110 MB），所以两个估计取大、
        并且窗口内保有过往最大值，过期再丢。RSS 也会因缓存与工件上涨，因此偏保守（高估
        成本 ⇒ 少放人）是可接受的方向。
        """
        if worker_memory_bytes is None:
            return
        try:
            rss = int(worker_memory_bytes)
        except (TypeError, ValueError):
            return
        if rss <= 0:
            return
        running = max(0, int(running_slots))
        if running == 0 and (self._entry_idle_memory_bytes <= 0 or rss < self._entry_idle_memory_bytes):
            self._entry_idle_memory_bytes = rss
        samples = self._entry_memory_samples
        samples.append((float(current_mono), running, rss))
        cutoff = float(current_mono) - _ENTRY_MEMORY_SAMPLE_WINDOW_SECONDS
        while len(samples) > 1 and samples[0][0] < cutoff:
            samples.popleft()
        estimates: list[int] = []
        low = min(samples, key=lambda item: item[1])
        high = max(samples, key=lambda item: item[1])
        slot_span = high[1] - low[1]
        if slot_span >= _ENTRY_MEMORY_MIN_SPAN_SLOTS and (high[2] - low[2]) >= _ENTRY_MEMORY_MIN_DELTA_BYTES:
            estimates.append(int((high[2] - low[2]) // slot_span))
        if running >= _ENTRY_MEMORY_MIN_AVG_SLOTS and 0 < self._entry_idle_memory_bytes < rss:
            estimates.append(int((rss - self._entry_idle_memory_bytes) // running))
        if estimates:
            accepted = [
                value for value in estimates
                if _ENTRY_SLOT_MEMORY_MIN_BYTES <= value <= _ENTRY_SLOT_MEMORY_MAX_BYTES
            ]
            if accepted:
                self._entry_slot_memory_estimates.append((float(current_mono), max(accepted)))
        estimate_cutoff = float(current_mono) - _ENTRY_MEMORY_ESTIMATE_WINDOW_SECONDS
        while len(self._entry_slot_memory_estimates) > 1 and self._entry_slot_memory_estimates[0][0] < estimate_cutoff:
            self._entry_slot_memory_estimates.popleft()
        if self._entry_slot_memory_estimates:
            self._entry_slot_memory_bytes = max(
                value for _mono, value in self._entry_slot_memory_estimates
            )

    def _entry_memory_slot_ceiling(
        self,
        *,
        machine_memory_total_bytes: Any,
        machine_memory_available_bytes: Any,
        running_slots: int,
    ) -> int | None:
        """内存还能容下多少格（含已在飞的那些）。保留量直接取机器内存 warn 线之上的那一段，
        不新设阈值。拿不到读数时返回 None＝这条轴不参与约束。"""
        try:
            total = int(machine_memory_total_bytes or 0)
            available = int(machine_memory_available_bytes or 0)
        except (TypeError, ValueError):
            return None
        if total <= 0 or available < 0:
            return None
        warn_percent = min(99.0, max(1.0, float(self._machine_memory_warn_percent or 88.0)))
        reserve_bytes = int(total * (100.0 - warn_percent) / 100.0)
        usable_bytes = max(0, available - reserve_bytes)
        slot_bytes = max(_ENTRY_SLOT_MEMORY_MIN_BYTES, int(self._entry_slot_memory_bytes or 0))
        return int(running_slots) + int(usable_bytes // slot_bytes)

    def _update_entry_throughput(self, *, current_mono: float, rate_pressure: dict[str, Any] | None) -> float:
        """把"本进程每分钟实际发起的模型请求数"记进滑动窗口，返回窗口均值。

        回合闸的**增长判据不再用它**：这一条量的是"本进程每分钟发起了多少发"，429 风暴里
        失败重试同样计入（10-12 实测为完成量的 3–4 倍），只作观测。判据见
        `_update_entry_completions`。
        """
        raw = dict(rate_pressure or {}).get('rolling_rpm_60s_sum')
        try:
            rpm = max(0.0, float(raw if raw is not None else 0.0))
        except (TypeError, ValueError):
            rpm = 0.0
        samples = self._entry_rpm_samples
        samples.append((float(current_mono), rpm))
        cutoff = float(current_mono) - max(_ENTRY_THROUGHPUT_WINDOW_SECONDS, 2.0 * _ENTRY_BEAT_SECONDS)
        while len(samples) > 1 and samples[0][0] < cutoff:
            samples.popleft()
        window_start = float(current_mono) - _ENTRY_THROUGHPUT_WINDOW_SECONDS
        recent = [value for mono, value in samples if mono >= window_start]
        if not recent:
            return rpm
        return sum(recent) / len(recent)

    def _update_entry_completions(self, *, current_mono: float, rate_pressure: dict[str, Any] | None) -> float:
        """本进程每分钟**换来了回答**的模型调用数，窗口均值——抬闸的收益按这个算。

        读不到完成量读数时（老 balancer、测试桩）退回发起量，保持这条判据在缺字段时
        仍然是"能动的"，而不是把闸永久钉死。
        """
        payload = dict(rate_pressure or {})
        raw = payload.get('rolling_completions_60s_sum')
        if raw is None:
            raw = payload.get('rolling_rpm_60s_sum')
        try:
            completions = max(0.0, float(raw if raw is not None else 0.0))
        except (TypeError, ValueError):
            completions = 0.0
        samples = self._entry_completion_samples
        samples.append((float(current_mono), completions))
        cutoff = float(current_mono) - max(_ENTRY_THROUGHPUT_WINDOW_SECONDS, 2.0 * _ENTRY_BEAT_SECONDS)
        while len(samples) > 1 and samples[0][0] < cutoff:
            samples.popleft()
        window_start = float(current_mono) - _ENTRY_THROUGHPUT_WINDOW_SECONDS
        recent = [value for mono, value in samples if mono >= window_start]
        if not recent:
            return completions
        return sum(recent) / len(recent)

    def _decide_entry_targets(
        self,
        *,
        controller_snapshot: dict[str, Any],
        current_mono: float,
        event_loop_lag_ms: float,
        writer_queue_depth: int,
        sqlite_write_wait_ms: float,
        sqlite_query_latency_ms: float,
        rate_pressure: dict[str, Any] | None,
        machine_memory_total_bytes: Any,
        machine_memory_available_bytes: Any,
        completion_rpm: float,
        loop_backlog_count: float = 0.0,
    ) -> dict[str, int]:
        """按节拍决定共享池的目标位：一次移动之后隔满一个反馈周期才允许再动。

        - 任一**本地**积压轴越过 critical 线 ⇒ 每节拍退一格，一律止于地板；越过 warn 线
          但没到 critical ⇒ 保持当前格数。
        - 上游限流（429 惩罚）**不再参与回合闸的否决**（操作者 2026-10-12 定）：限流恶化由
          模型链自己的退避/换槽车道收口，不由容量收口。实盘依据是 10-12 那次分发事故——
          `penalty_429_max` 全程 12.5–23.3 让闸被钉在地板 40 多分钟、execution 排队 15–30，
          而同期完成量 22–36 次/分。这条轴把"该不该给格子"和"上游此刻吃不吃得下"混成了
          一个判断，而后者本来就有更便宜、更靠得住的收口处。
        - 抬格前提＝闸确实卡住需求 + 本地轴有余量；在此之上两条放行路任一成立即可扩：
          ① 这一拍的**完成量**比上次抬闸那一刻高（边际收益，山脊搜索）；② 满载且有等待
          已连续超过 `_ENTRY_WAIT_GROWTH_SECONDS`（等久了先放人，收益由①在下一拍复核）。
          判据的分子从"发起了多少发"换成"换来了多少回答"：10-12 实测前者是后者的 3–4 倍
          （rpm 90–138 vs 完成 22–36 次/分），拿它判边际收益等于拿重试当吞吐。
        - 内存余量与 `_ENTRY_POOL_MAX_FACTOR` 硬顶仍然否决上冲：凌晨那次 90 秒爬到 27 格、
          Private 2.4 GB 的形态不因为换了触发条件就重新变得可接受。
        """
        backlog_ratio = min(
            self._entry_axis_ratio(self._event_loop_warn_ms, event_loop_lag_ms),
            self._entry_axis_ratio(float(self._writer_queue_warn), writer_queue_depth),
            self._entry_axis_ratio(self._sqlite_write_wait_warn_ms, sqlite_write_wait_ms),
            self._entry_axis_ratio(self._sqlite_query_warn_ms, sqlite_query_latency_ms),
            # 第五根积压轴＝回环连接条数。它量的是"内核还能不能再开一条套接字"，
            # 10055 是它耗尽之后的事后异常，实测只有 0.25 次/分，等它响已经太晚。
            self._entry_axis_ratio(_ENTRY_LOOPBACK_WARN_CONNECTIONS, loop_backlog_count),
        )
        # 上游限流惩罚不参与这里的判断（见上面文档），它仍会作为
        # `model_rate_penalty_429_max` 写进快照供事后判读。
        ratio = backlog_ratio
        critical = (
            float(event_loop_lag_ms or 0.0) >= self._event_loop_critical_ms
            or int(writer_queue_depth or 0) >= self._writer_queue_critical
            or float(sqlite_write_wait_ms or 0.0) >= self._sqlite_write_wait_critical_ms
            or float(sqlite_query_latency_ms or 0.0) >= self._sqlite_query_critical_ms
            or float(loop_backlog_count or 0.0) >= _ENTRY_LOOPBACK_CRITICAL_CONNECTIONS
        )
        pool = dict(controller_snapshot.get('entry_gate_pool') or {})
        role_limits = dict(controller_snapshot.get('entry_gate_limit') or {})
        role_ceilings = dict(controller_snapshot.get('entry_gate_ceiling') or {})
        running_by_role = dict(controller_snapshot.get('entry_gate_running') or {})
        queued_by_role = dict(controller_snapshot.get('entry_gate_queued') or {})
        # 读不到池真值时（老控制器/测试桩）按各角色之和回算，保持同一口径而不是各自封顶。
        pool_ceiling = int(pool.get('ceiling') or 0) or max(
            1, sum(int(value or 1) for value in role_ceilings.values())
        )
        pool_running = int(pool.get('running') or 0) or sum(int(value or 0) for value in running_by_role.values())
        pool_queued = int(pool.get('queued') or 0) or sum(int(value or 0) for value in queued_by_role.values())
        current = int(pool.get('limit') or 0) or max(1, sum(int(value or 0) for value in role_limits.values()))
        current = max(1, current)
        memory_ceiling = self._entry_memory_slot_ceiling(
            machine_memory_total_bytes=machine_memory_total_bytes,
            machine_memory_available_bytes=machine_memory_available_bytes,
            running_slots=pool_running,
        )
        beat_due = (float(current_mono) - self._entry_last_step_at) >= _ENTRY_BEAT_SECONDS
        binding = pool_queued > 0 or pool_running >= current
        if binding:
            if self._entry_binding_since_mono <= 0.0:
                self._entry_binding_since_mono = float(current_mono)
            binding_sustained = (
                float(current_mono) - self._entry_binding_since_mono
            ) >= _ENTRY_WAIT_GROWTH_SECONDS
        else:
            self._entry_binding_since_mono = 0.0
            binding_sustained = False
        headroom = ratio > 1.0
        previous_growth_bar = self._entry_completion_bar_at_last_growth
        stale_bar = (current_mono - self._entry_rpm_bar_set_at) >= _ENTRY_PROBE_AFTER_SECONDS
        productive = (
            previous_growth_bar is None
            or stale_bar
            or completion_rpm >= float(previous_growth_bar) + _ENTRY_MIN_THROUGHPUT_GAIN
        )
        grow_now = beat_due and binding and headroom and not critical and (productive or binding_sustained)

        targets: dict[str, int] = {}
        if not beat_due:
            targets[_ENTRY_POOL_KEY] = current
        elif critical or not headroom or (binding and not productive and not binding_sustained):
            # 退让永远止于地板：地板就是操作员给的"最少也要这么多"。实盘教训（22:50 重启后）
            # 是 lag 常态 1.3–6.7 s（Qoder 占满机器 + 185 个节点在闸口等物化），当时把
            # critical 授权成下穿地板，结果 4 个节拍里 8→5 且一路往下，模型调用行从
            # 2–14 次/分直接归零——闸越收，排队越长，lag 越高，形成自锁。
            # 真正能一次踩到 0 的只有磁盘紧急那把硬闸（controller 侧）。
            targets[_ENTRY_POOL_KEY] = max(pool_ceiling, current - _ENTRY_SHRINK_SLOTS_PER_BEAT)
        elif grow_now:
            candidate = min(
                current + _ENTRY_GROWTH_SLOTS_PER_BEAT,
                max(current, int(current * ratio)),
                # 与地板挂钩的硬顶：完成量这条轴历史上被重试污染过，"等久了就扩"不能只靠
                # 内存读数兜底（10-12 一格实测 14.6 MB，可用 1.8 GB ⇒ 内存轴会放行到 100+ 格）。
                max(pool_ceiling, int(pool_ceiling * _ENTRY_POOL_MAX_FACTOR)),
            )
            if memory_ceiling is None:
                # 读不到内存读数时不许越过地板：v1 正是在"内存读数滞后/不可用"的状态下
                # 90 秒爬到 27 格、把 worker Private 推到 2.4 GB。
                candidate = pool_ceiling
            else:
                candidate = max(pool_ceiling, min(candidate, memory_ceiling))
            targets[_ENTRY_POOL_KEY] = max(current, candidate)
        else:
            targets[_ENTRY_POOL_KEY] = current

        moved = int(targets[_ENTRY_POOL_KEY]) != current
        if beat_due and moved:
            self._entry_last_step_at = float(current_mono)
            if grow_now:
                self._entry_completion_bar_at_last_growth = float(completion_rpm)
            # 只要这一节拍做过判断（抬成、抬不动、或紧急退让），就重置计时：
            # 试探性抬格的间隔因此是 `_ENTRY_PROBE_AFTER_SECONDS` 而不是每个节拍，
            # 避免"退一格→基准过期→又抬两格"的短周期来回。
            self._entry_rpm_bar_set_at = float(current_mono)
        # 角色键继续回显池位，供既有面板与读数使用（角色不再各自封顶，别把它读成上限）。
        for role in dict(controller_snapshot.get('entry_gate_running') or {}):
            targets[str(role)] = int(targets[_ENTRY_POOL_KEY])
        return targets

    def observe_sample(
        self,
        *,
        machine_cpu_percent: float,
        machine_memory_percent: float,
        machine_disk_busy_percent: float,
        machine_available: bool,
        disk_busy_available: bool = True,
        disk_read_bytes_per_sec: float = 0.0,
        disk_write_bytes_per_sec: float = 0.0,
        event_loop_lag_ms: float,
        writer_queue_depth: int,
        sqlite_write_wait_ms: float,
        sqlite_query_latency_ms: float,
        process_cpu_ratio: float,
        now_mono: float | None = None,
        now_iso: str | None = None,
        machine_disk_free_bytes: int | None = None,
        machine_disk_total_bytes: int | None = None,
        machine_memory_total_bytes: int | None = None,
        machine_memory_available_bytes: int | None = None,
        worker_memory_bytes: int | None = None,
        rate_pressure: dict[str, Any] | None = None,
        loop_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_mono = float(now_mono if now_mono is not None else time.perf_counter())
        timestamp = str(now_iso or _now_iso()).strip() or _now_iso()
        controller_snapshot = self._controller.snapshot()
        waiting_count = int(controller_snapshot.get('worker_execution_waiting_count') or 0)
        oldest_wait_ms = float(controller_snapshot.get('worker_execution_oldest_wait_ms') or 0.0)
        machine_available_bool = bool(machine_available)
        machine_warn = (
            machine_available_bool
            and (
                float(machine_cpu_percent or 0.0) >= self._machine_cpu_warn_percent
                or float(machine_memory_percent or 0.0) >= self._machine_memory_warn_percent
                or (bool(disk_busy_available) and float(machine_disk_busy_percent or 0.0) >= self._machine_disk_busy_warn_percent)
            )
        )
        machine_critical = (
            machine_available_bool
            and (
                float(machine_cpu_percent or 0.0) >= self._machine_cpu_critical_percent
                or float(machine_memory_percent or 0.0) >= self._machine_memory_critical_percent
                or (bool(disk_busy_available) and float(machine_disk_busy_percent or 0.0) >= self._machine_disk_busy_critical_percent)
            )
        )
        machine_safe = (
            bool(machine_available)
            and float(machine_cpu_percent or 0.0) <= self._machine_cpu_safe_percent
            and float(machine_memory_percent or 0.0) <= self._machine_memory_safe_percent
            and (
                not bool(disk_busy_available)
                or float(machine_disk_busy_percent or 0.0) <= self._machine_disk_busy_safe_percent
            )
        )
        local_degraded = (
            float(event_loop_lag_ms or 0.0) >= self._event_loop_warn_ms
            or int(writer_queue_depth or 0) >= self._writer_queue_warn
            or float(sqlite_write_wait_ms or 0.0) >= self._sqlite_write_wait_warn_ms
            or float(sqlite_query_latency_ms or 0.0) >= self._sqlite_query_warn_ms
            or float(process_cpu_ratio or 0.0) >= self._process_cpu_warn_ratio
        )
        local_critical = (
            (
                float(event_loop_lag_ms or 0.0) >= self._event_loop_critical_ms
                and waiting_count > 0
                and waiting_count > self._last_waiting_count
            )
            or int(writer_queue_depth or 0) >= self._writer_queue_critical
            or float(sqlite_write_wait_ms or 0.0) >= self._sqlite_write_wait_critical_ms
            or float(sqlite_query_latency_ms or 0.0) >= self._sqlite_query_critical_ms
        )
        local_safe = (
            float(event_loop_lag_ms or 0.0) <= self._event_loop_safe_ms
            and int(writer_queue_depth or 0) <= self._writer_queue_safe
            and float(sqlite_write_wait_ms or 0.0) <= self._sqlite_write_wait_safe_ms
            and float(sqlite_query_latency_ms or 0.0) <= self._sqlite_query_safe_ms
            and float(process_cpu_ratio or 0.0) <= self._process_cpu_safe_ratio
        )
        machine_state = 'unknown'
        if machine_critical:
            machine_state = 'critical'
        elif machine_warn:
            machine_state = 'warn'
        elif machine_safe:
            machine_state = 'normal'
        local_state = 'critical' if local_critical else ('degraded' if local_degraded else ('normal' if local_safe else 'elevated'))
        pending_disk_hooks: list[Callable[[], None]] = []
        disk_free = int(machine_disk_free_bytes) if machine_disk_free_bytes is not None and int(machine_disk_free_bytes) >= 0 else -1
        disk_total = int(machine_disk_total_bytes) if machine_disk_total_bytes is not None and int(machine_disk_total_bytes) > 0 else -1
        disk_watermark_available = disk_free >= 0 and disk_total > 0
        disk_usage_percent = round((disk_total - disk_free) / disk_total * 100.0, 3) if disk_watermark_available else 0.0
        with self._lock:
            self._sample_mono = current_mono
            self._snapshot = {
                'machine_pressure_available': bool(machine_available),
                'machine_pressure_cpu_percent': round(max(0.0, float(machine_cpu_percent or 0.0)), 3),
                'machine_pressure_memory_percent': round(max(0.0, float(machine_memory_percent or 0.0)), 3),
                'machine_pressure_disk_busy_percent': round(max(0.0, float(machine_disk_busy_percent or 0.0)), 3),
                'machine_pressure_disk_busy_available': bool(disk_busy_available),
                'machine_pressure_disk_read_bytes_per_sec': round(max(0.0, float(disk_read_bytes_per_sec or 0.0)), 3),
                'machine_pressure_disk_write_bytes_per_sec': round(max(0.0, float(disk_write_bytes_per_sec or 0.0)), 3),
                'machine_disk_free_bytes': disk_free,
                'machine_disk_total_bytes': disk_total,
                'machine_disk_usage_percent': disk_usage_percent,
                'tool_pressure_event_loop_lag_ms': round(max(0.0, float(event_loop_lag_ms or 0.0)), 3),
                'tool_pressure_writer_queue_depth': int(max(0, int(writer_queue_depth or 0))),
                'tool_pressure_process_cpu_ratio': round(max(0.0, float(process_cpu_ratio or 0.0)), 4),
                'sqlite_write_wait_ms': round(max(0.0, float(sqlite_write_wait_ms or 0.0)), 3),
                'sqlite_query_latency_ms': round(max(0.0, float(sqlite_query_latency_ms or 0.0)), 3),
                'machine_pressure_state': machine_state,
                'local_pressure_state': local_state,
                'pressure_sample_at': timestamp,
                'tool_pressure_sample_at': timestamp,
            }
            # 磁盘治理（P1）：水位决策（防抖：进入 N 连续样本、恢复 M 连续样本）。
            # 边沿触发 controller 硬闸与钩子；钩子只收集、出锁后调用（不阻塞采样）。
            if disk_watermark_available:
                policies = disk_policies()
                emg_now = disk_free < emergency_threshold_bytes(disk_total, policies=policies)
                if emg_now:
                    self._disk_emergency_streak += 1
                    self._disk_recovery_streak = 0
                else:
                    self._disk_emergency_streak = 0
                if not self._disk_emergency_active and self._disk_emergency_streak >= max(1, int(policies.emergency_streak_samples)):
                    self._disk_emergency_active = True
                    self._disk_emergency_since = timestamp
                    try:
                        self._controller.set_disk_emergency(True, at=timestamp)
                    except Exception:
                        pass
                    if self._disk_emergency_enter_hook is not None:
                        pending_disk_hooks.append(self._disk_emergency_enter_hook)
                elif self._disk_emergency_active and not emg_now:
                    self._disk_recovery_streak += 1
                    if self._disk_recovery_streak >= max(1, int(policies.emergency_recovery_samples)):
                        self._disk_emergency_active = False
                        self._disk_emergency_since = ''
                        self._disk_recovery_streak = 0
                        try:
                            self._controller.set_disk_emergency(False, at=timestamp)
                        except Exception:
                            pass
                        if self._disk_emergency_exit_hook is not None:
                            pending_disk_hooks.append(self._disk_emergency_exit_hook)
            self._snapshot['disk_emergency_active'] = bool(self._disk_emergency_active)
            self._snapshot['disk_emergency_since'] = self._disk_emergency_since
            if machine_state in {'warn', 'critical'}:
                self._consecutive_machine_warn += 1
                self._consecutive_machine_safe = 0
            elif machine_safe:
                self._consecutive_machine_safe += 1
                self._consecutive_machine_warn = 0
            else:
                self._consecutive_machine_warn = 0
                self._consecutive_machine_safe = 0
            if local_critical:
                self._consecutive_local_critical += 1
                self._consecutive_local_safe = 0
            else:
                self._consecutive_local_critical = 0
                if local_safe:
                    self._consecutive_local_safe += 1
                else:
                    self._consecutive_local_safe = 0

            current_state = str(self._controller.snapshot().get('tool_pressure_state') or 'normal')
            should_critical = bool(machine_critical or local_critical)
            should_throttle = machine_warn and self._consecutive_machine_warn >= self._warn_consecutive_samples
            machine_recovery = machine_safe and self._consecutive_machine_safe >= self._safe_consecutive_samples
            # C: local-driven recovery. Local health is sufficient to ease when the machine
            # is not actively pressuring (also covers machine_state 'unknown' when machine
            # metrics are unavailable).
            local_recovery_ready = (
                bool(self._local_recovery_enabled)
                and self._consecutive_local_safe >= self._safe_consecutive_samples
                and not machine_warn
                and not machine_critical
            )
            should_ease = machine_recovery or local_recovery_ready

            # A+B: escape valves for one-way throttling. Computed AFTER should_critical so
            # forced easing can never override an active critical sample.
            restricted_state = current_state in {'critical', 'throttled'}
            if restricted_state and self._restricted_since_mono <= 0.0:
                # Defensive: start timing a restricted episode we did not cause.
                self._restricted_since_mono = current_mono
            dwell_forced = (
                self._max_pressure_dwell_seconds > 0.0
                and restricted_state
                and self._restricted_since_mono > 0.0
                and (current_mono - self._restricted_since_mono) >= self._max_pressure_dwell_seconds
            )
            starvation_forced = (
                self._max_tool_wait_ms > 0.0
                and oldest_wait_ms >= self._max_tool_wait_ms
                and current_state in {'critical', 'throttled', 'easing'}
                and (
                    restricted_state
                    or current_mono - self._last_recovery_step_at >= self._recover_window_seconds
                )
            )
            forced_ease = (not should_critical) and (dwell_forced or starvation_forced)
            heal_reason = ''

            # 磁盘紧急硬闸激活期间冻结整条压力决策链：critical()/step_easing()/
            # set_budget_state('normal') 都不得把 target_limit 从 0 抬起（否则
            # dwell/starvation 逃逸阀每 30-60s 会反杀紧急态，放出新工具调用）。
            if not self._disk_emergency_active:
                if should_critical:
                    self._controller.critical(at=timestamp)
                    self._last_recovery_step_at = current_mono
                    if self._restricted_since_mono <= 0.0:
                        self._restricted_since_mono = current_mono
                elif forced_ease:
                    heal_reason = 'dwell_timeout' if dwell_forced else 'starvation'
                    if waiting_count > 0:
                        # One concrete recovery step (+1 target slot, drains oldest waiter)
                        # regardless of machine_safe. step_easing (not begin_easing) because
                        # begin_easing neither raises the limit nor drains waiters.
                        self._controller.step_easing(at=timestamp)
                        self._last_recovery_step_at = current_mono
                    elif current_state != 'normal':
                        self._controller.set_budget_state('normal', at=timestamp)
                        self._last_recovery_step_at = 0.0
                    self._restricted_since_mono = 0.0
                    # Hysteresis: require a fresh warn streak before re-throttling so the
                    # forced step is not immediately undone on the next sample.
                    self._consecutive_machine_warn = 0
                elif should_throttle:
                    self._controller.throttle(at=timestamp)
                    self._last_recovery_step_at = current_mono
                    if self._restricted_since_mono <= 0.0:
                        self._restricted_since_mono = current_mono
                elif should_ease:
                    if local_recovery_ready and not machine_recovery:
                        heal_reason = 'local_recovery'
                    if waiting_count > 0:
                        if current_state != 'easing':
                            self._controller.begin_easing(at=timestamp)
                            self._last_recovery_step_at = current_mono
                        elif current_mono - self._last_recovery_step_at >= self._recover_window_seconds:
                            self._controller.step_easing(at=timestamp)
                            self._last_recovery_step_at = current_mono
                    elif current_state != 'normal':
                        self._controller.set_budget_state('normal', at=timestamp)
                        self._last_recovery_step_at = 0.0
                    self._restricted_since_mono = 0.0
                elif current_state == 'easing' and waiting_count <= 0:
                    self._controller.set_budget_state('normal', at=timestamp)
                    self._last_recovery_step_at = 0.0
            # 回合闸：不看工具压力状态，只看自家积压与内存余量算出来的目标。
            # 工具槽的 normal/easing/throttled 量的是"一次工具调用"的积压，拿它当节点并发
            # 的上限会把两者绑死（09-30 实盘：一次 141 秒 SQLite 读让 budget 落 critical，
            # 双闸踩到 1/1 约 9 分钟，而期间内存与上游都还空着）。
            entry_running_total = sum(
                int(value or 0) for value in (controller_snapshot.get('entry_gate_running') or {}).values()
            )
            self._update_entry_slot_memory(
                current_mono=current_mono,
                running_slots=entry_running_total,
                worker_memory_bytes=worker_memory_bytes,
            )
            throughput_rpm = self._update_entry_throughput(
                current_mono=current_mono,
                rate_pressure=rate_pressure,
            )
            completion_rpm = self._update_entry_completions(
                current_mono=current_mono,
                rate_pressure=rate_pressure,
            )
            entry_targets = self._decide_entry_targets(
                controller_snapshot=controller_snapshot,
                current_mono=current_mono,
                event_loop_lag_ms=event_loop_lag_ms,
                writer_queue_depth=writer_queue_depth,
                sqlite_write_wait_ms=sqlite_write_wait_ms,
                sqlite_query_latency_ms=sqlite_query_latency_ms,
                rate_pressure=rate_pressure,
                machine_memory_total_bytes=machine_memory_total_bytes,
                machine_memory_available_bytes=machine_memory_available_bytes,
                completion_rpm=completion_rpm,
                loop_backlog_count=float(dict(loop_stats or {}).get('loopback_established_count') or 0.0),
            )
            entry_limits = self._controller.set_entry_targets(entry_targets)
            self._snapshot['entry_gate_targets'] = dict(entry_targets)
            self._snapshot['entry_gate_limits'] = dict(entry_limits)
            # 池子才是容量真值；角色那两份（各自保留格、各自在跑/在等）留给判读"谁在等"。
            self._snapshot['entry_gate_pool'] = dict(controller_snapshot.get('entry_gate_pool') or {})
            self._snapshot['entry_gate_reserved'] = dict(controller_snapshot.get('entry_gate_reserved') or {})
            self._snapshot['entry_gate_running_total'] = int(entry_running_total)
            self._snapshot['entry_slot_memory_bytes'] = int(self._entry_slot_memory_bytes)
            self._snapshot['entry_idle_memory_bytes'] = int(self._entry_idle_memory_bytes)
            self._snapshot['entry_throughput_rpm_60s'] = round(float(throughput_rpm), 3)
            self._snapshot['entry_completions_60s'] = round(float(completion_rpm), 3)
            self._snapshot['entry_completions_at_last_growth'] = (
                None
                if self._entry_completion_bar_at_last_growth is None
                else round(float(self._entry_completion_bar_at_last_growth), 3)
            )
            # 等待驱动的扩容通道必须能读回触发条件本身：满载且有等待已经连续多久了。
            # 没有这个数，"为什么这一拍放了 2 格"和"为什么那段没放"在历史里读不出来。
            self._snapshot['entry_binding_seconds'] = round(
                0.0
                if self._entry_binding_since_mono <= 0.0
                else max(0.0, current_mono - self._entry_binding_since_mono),
                3,
            )
            self._snapshot['entry_seconds_since_step'] = round(
                max(0.0, current_mono - self._entry_last_step_at), 3
            )
            self._snapshot['entry_loopback_established'] = int(
                float(dict(loop_stats or {}).get('loopback_established_count') or 0.0)
            )
            for _stat_key, _snapshot_key in (
                ('task_event_queued_count', 'entry_event_queue_depth'),
                ('task_event_dropped_count', 'entry_event_dropped_total'),
                ('task_event_batch_request_count', 'entry_event_batch_request_total'),
                ('task_event_batch_item_count', 'entry_event_batch_item_total'),
                ('task_event_single_fallback_count', 'entry_event_single_fallback_total'),
                ('task_event_coalesced_count', 'entry_event_coalesced_total'),
                ('loopback_pool_connections', 'entry_loopback_pool_total'),
                ('loopback_pool_leased', 'entry_loopback_pool_leased'),
                ('callback_delivery_error_count', 'entry_callback_error_total'),
                ('task_event_dropped_coalesced_count', 'entry_event_dropped_coalesced_total'),
                ('task_event_dropped_other_count', 'entry_event_dropped_other_total'),
                ('task_event_queue_bytes_estimated', 'entry_event_queue_bytes_estimated'),
            ):
                if _stat_key in (loop_stats or {}):
                    self._snapshot[_snapshot_key] = int(float(loop_stats.get(_stat_key) or 0.0))
            self._snapshot['worker_memory_bytes'] = (
                int(worker_memory_bytes) if worker_memory_bytes is not None and int(worker_memory_bytes) >= 0 else -1
            )
            self._snapshot['machine_memory_available_bytes'] = (
                int(machine_memory_available_bytes)
                if machine_memory_available_bytes is not None and int(machine_memory_available_bytes) >= 0
                else -1
            )
            self._snapshot['machine_memory_total_bytes'] = (
                int(machine_memory_total_bytes)
                if machine_memory_total_bytes is not None and int(machine_memory_total_bytes) > 0
                else -1
            )
            self._snapshot['model_rate_penalty_429_max'] = round(
                float(dict(rate_pressure or {}).get('penalty_429_max') or 0.0), 6
            )
            self._snapshot['model_rolling_rpm_60s_max'] = round(
                float(dict(rate_pressure or {}).get('rolling_rpm_60s_max') or 0.0), 3
            )
            self._snapshot['model_rolling_rpm_60s_sum'] = int(
                float(dict(rate_pressure or {}).get('rolling_rpm_60s_sum') or 0.0)
            )
            self._snapshot['budget_state'] = str(self._controller.snapshot().get('tool_pressure_state') or 'normal')
            self._snapshot['tool_pressure_self_heal_reason'] = heal_reason
            self._last_waiting_count = waiting_count
        for hook in pending_disk_hooks:
            try:
                hook()
            except Exception:
                pass
        return self.snapshot()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop = asyncio.get_running_loop()
        self._lag_sampler = _EventLoopLagSampler(self._loop)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name='task-worker-pressure-monitor',
            daemon=True,
        )
        self._thread.start()

    async def close(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        self._stop_event.set()
        await asyncio.to_thread(thread.join, 5.0)

    def _thread_main(self) -> None:
        last_wall = time.perf_counter()
        last_cpu = time.process_time()
        while not self._stop_event.wait(self._sample_seconds):
            current_wall = time.perf_counter()
            current_cpu = time.process_time()
            wall_delta = max(1e-6, current_wall - last_wall)
            cpu_delta = max(0.0, current_cpu - last_cpu)
            lag_sampler = self._lag_sampler
            # 先取读数、再发探针：ping() 记的 `sent_at` 晚于本拍的 current_wall，
            # 拿 ping 之前的时刻去差就永远是负数，被 max(0,·) 截成 0——实测后果是
            # 「250 ms–1 s」这一整段真实滞后读成 0（warn 线 250 ms 落在这段里），
            # 只有滞后跨过整整一个节拍（≥1 s）才会露出来。
            event_loop_lag_ms = lag_sampler.sample() if lag_sampler is not None else 0.0
            if lag_sampler is not None:
                lag_sampler.ping()
            runtime_metrics = self._runtime_metrics_snapshot()
            machine = self._sample_machine_metrics(current_wall)
            machine.update(self._disk_waterline_fields())
            machine.update(self._memory_bytes_fields(machine))
            try:
                self.observe_sample(
                    machine_cpu_percent=float(machine.get('cpu_percent') or 0.0),
                    machine_memory_percent=float(machine.get('memory_percent') or 0.0),
                    machine_disk_busy_percent=float(machine.get('disk_busy_percent') or 0.0),
                    machine_available=bool(machine.get('available')),
                    disk_busy_available=bool(machine.get('disk_busy_available')),
                    disk_read_bytes_per_sec=float(machine.get('disk_read_bytes_per_sec') or 0.0),
                    disk_write_bytes_per_sec=float(machine.get('disk_write_bytes_per_sec') or 0.0),
                    event_loop_lag_ms=event_loop_lag_ms,
                    writer_queue_depth=int(runtime_metrics.get('writer_queue_depth') or 0),
                    sqlite_write_wait_ms=float(runtime_metrics.get('sqlite_write_wait_ms') or 0.0),
                    sqlite_query_latency_ms=float(runtime_metrics.get('sqlite_query_latency_ms') or 0.0),
                    process_cpu_ratio=(cpu_delta / wall_delta),
                    now_mono=current_wall,
                    machine_disk_free_bytes=machine.get('disk_free_bytes'),
                    machine_disk_total_bytes=machine.get('disk_total_bytes'),
                    machine_memory_total_bytes=machine.get('memory_total_bytes'),
                    machine_memory_available_bytes=machine.get('memory_available_bytes'),
                    worker_memory_bytes=self._sample_worker_memory(),
                    rate_pressure=self._sample_rate_pressure(),
                    loop_stats=self._sample_loop_pressure(),
                )
            except Exception:
                time.sleep(min(1.0, self._sample_seconds))
            last_wall = current_wall
            last_cpu = current_cpu

    def _memory_bytes_fields(self, existing: dict[str, Any]) -> dict[str, Any]:
        """机器内存的字节读数：注入的采样器给了就沿用，没给就从 psutil 补一次。

        回合闸的内存轴需要 available 而不只是 percent——percent 是滞后的旁观量，
        01:24 实测 limit 涨到 11 那一拍机器内存才 73.7%，等它爬到 94.8% 已经晚了。
        """
        if existing.get('memory_total_bytes') is not None and existing.get('memory_available_bytes') is not None:
            return {}
        if psutil is None:
            return {'memory_total_bytes': None, 'memory_available_bytes': None}
        try:
            virtual = psutil.virtual_memory()
        except Exception:
            return {'memory_total_bytes': None, 'memory_available_bytes': None}
        return {
            'memory_total_bytes': int(getattr(virtual, 'total', 0) or 0) or None,
            'memory_available_bytes': int(getattr(virtual, 'available', 0) or 0) or None,
        }

    def _sample_worker_memory(self) -> int | None:
        """本进程 RSS（回合闸内存轴的成本样本）。句柄建一次，读失败就交回 None。"""
        if psutil is None:
            return None
        handle = self._process_handle
        if handle is None:
            try:
                handle = psutil.Process()
            except Exception:
                return None
            self._process_handle = handle
        try:
            return int(handle.memory_info().rss)
        except Exception:
            return None

    def _sample_rate_pressure(self) -> dict[str, Any]:
        observer = self._rate_limit_observer
        if not callable(observer):
            return {}
        try:
            return dict(observer() or {})
        except Exception:
            return {}

    def _sample_loop_pressure(self) -> dict[str, Any]:
        observer = self._loop_observer
        if not callable(observer):
            return {}
        try:
            return dict(observer() or {})
        except Exception:
            return {}

    def _disk_waterline_fields(self) -> dict[str, Any]:
        """磁盘剩余空间采样（disk_guard 带 TTL 缓存，不逐 tick statfs）。"""
        with self._lock:
            paths = tuple(self._disk_watermark_paths)
        if not paths:
            return {'disk_free_bytes': None, 'disk_total_bytes': None}
        try:
            snapshot = disk_waterline_snapshot(paths)
        except Exception:
            return {'disk_free_bytes': None, 'disk_total_bytes': None}
        if snapshot is None:
            return {'disk_free_bytes': None, 'disk_total_bytes': None}
        free, total = snapshot
        return {'disk_free_bytes': int(free), 'disk_total_bytes': int(total)}

    def _runtime_metrics_snapshot(self) -> dict[str, Any]:
        snapshot_getter = getattr(self._store, 'runtime_metrics_snapshot', None)
        if callable(snapshot_getter):
            try:
                payload = dict(snapshot_getter() or {})
            except Exception:
                payload = {}
        else:
            payload = {}
        if 'writer_queue_depth' not in payload:
            try:
                payload['writer_queue_depth'] = int(getattr(self._store, 'writer_queue_depth', lambda: 0)() or 0)
            except Exception:
                payload['writer_queue_depth'] = 0
        return payload

    @staticmethod
    def _disk_busy_percent_from_samples(current_disk: Any, previous_disk: Any, wall_delta: float) -> tuple[bool, float]:
        current_busy_time = getattr(current_disk, 'busy_time', None)
        previous_busy_time = getattr(previous_disk, 'busy_time', None)
        if current_busy_time is not None and previous_busy_time is not None:
            busy_delta = max(0.0, float(current_busy_time - previous_busy_time))
            return True, min(100.0, busy_delta / (wall_delta * 1000.0) * 100.0)

        fallback_deltas: list[float] = []
        current_read_time = getattr(current_disk, 'read_time', None)
        previous_read_time = getattr(previous_disk, 'read_time', None)
        if current_read_time is not None and previous_read_time is not None:
            fallback_deltas.append(max(0.0, float(current_read_time - previous_read_time)))
        current_write_time = getattr(current_disk, 'write_time', None)
        previous_write_time = getattr(previous_disk, 'write_time', None)
        if current_write_time is not None and previous_write_time is not None:
            fallback_deltas.append(max(0.0, float(current_write_time - previous_write_time)))
        if not fallback_deltas:
            return False, 0.0
        busy_delta = max(fallback_deltas)
        return True, min(100.0, busy_delta / (wall_delta * 1000.0) * 100.0)

    def _sample_machine_metrics(self, now_mono: float) -> dict[str, Any]:
        sampler = self._system_metrics_sampler
        if callable(sampler):
            payload = dict(sampler() or {})
            payload.setdefault('available', True)
            payload.setdefault('disk_busy_available', True)
            return payload
        if psutil is None:
            return {
                'available': False,
                'disk_busy_available': False,
                'cpu_percent': 0.0,
                'memory_percent': 0.0,
                'disk_busy_percent': 0.0,
                'disk_read_bytes_per_sec': 0.0,
                'disk_write_bytes_per_sec': 0.0,
            }
        try:
            cpu_percent = float(psutil.cpu_percent(interval=None) or 0.0)
            memory_percent = float(getattr(psutil.virtual_memory(), 'percent', 0.0) or 0.0)
            disk = psutil.disk_io_counters()
        except Exception:
            return {
                'available': False,
                'disk_busy_available': False,
                'cpu_percent': 0.0,
                'memory_percent': 0.0,
                'disk_busy_percent': 0.0,
                'disk_read_bytes_per_sec': 0.0,
                'disk_write_bytes_per_sec': 0.0,
            }
        try:
            perdisk = psutil.disk_io_counters(perdisk=True)
        except Exception:
            perdisk = None
        disk_busy_percent = 0.0
        disk_busy_available = False
        disk_read_bytes_per_sec = 0.0
        disk_write_bytes_per_sec = 0.0
        if disk is not None and self._last_disk_sample is not None and self._last_disk_sample_mono > 0.0:
            wall_delta = max(1e-6, now_mono - self._last_disk_sample_mono)
            try:
                disk_read_bytes_per_sec = max(0.0, float(getattr(disk, 'read_bytes', 0) - getattr(self._last_disk_sample, 'read_bytes', 0)) / wall_delta)
                disk_write_bytes_per_sec = max(0.0, float(getattr(disk, 'write_bytes', 0) - getattr(self._last_disk_sample, 'write_bytes', 0)) / wall_delta)
            except Exception:
                disk_read_bytes_per_sec = 0.0
                disk_write_bytes_per_sec = 0.0
            disk_busy_available, disk_busy_percent = self._disk_busy_percent_from_samples(
                disk,
                self._last_disk_sample,
                wall_delta,
            )
            if (not disk_busy_available or disk_busy_percent <= 0.0) and isinstance(perdisk, dict) and isinstance(self._last_perdisk_sample, dict):
                per_disk_busy_values: list[float] = []
                for name, current_disk in perdisk.items():
                    previous_disk = self._last_perdisk_sample.get(name) if isinstance(self._last_perdisk_sample, dict) else None
                    if previous_disk is None:
                        continue
                    current_available, current_busy = self._disk_busy_percent_from_samples(
                        current_disk,
                        previous_disk,
                        wall_delta,
                    )
                    if current_available:
                        per_disk_busy_values.append(float(current_busy or 0.0))
                if per_disk_busy_values:
                    disk_busy_available = True
                    disk_busy_percent = max(per_disk_busy_values)
        self._last_disk_sample = disk
        self._last_perdisk_sample = dict(perdisk or {}) if isinstance(perdisk, dict) else None
        self._last_disk_sample_mono = now_mono
        return {
            'available': True,
            'disk_busy_available': disk_busy_available,
            'cpu_percent': cpu_percent,
            'memory_percent': memory_percent,
            'disk_busy_percent': disk_busy_percent,
            'disk_read_bytes_per_sec': disk_read_bytes_per_sec,
            'disk_write_bytes_per_sec': disk_write_bytes_per_sec,
        }


ToolPressureMonitor = WorkerPressureMonitor
