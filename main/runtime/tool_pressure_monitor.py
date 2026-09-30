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
# 只有这几组是新的，且都按 09-30 全天实测标定：
# - 轴比上下限：安静时一拍最多翻倍，最挤时一拍最多收到 1/4（实测 lag 见过 12.0 s、
#   一次 SQLite 读 141 s，靠变化率而不是靠跳幅兜底）。
_ENTRY_AXIS_FLOOR_RATIO = 0.25
_ENTRY_AXIS_CEILING_RATIO = 2.0
# - 上游限流惩罚：实测 penalty_429 p50=0 / p90=0.18 / p99=4.16 / max=13.57（6255 条路由观测）。
#   越过 p90 档只"保持"当前并发，越过 p99 档才开始缓减。
_ENTRY_RATE_HOLD_PENALTY = 1.0
_ENTRY_RATE_STEP_DOWN_PENALTY = 4.0
# - 单格内存成本的兜底值：实测 9 格在飞时 worker RSS 1.04 GB，凌晨 27 格时 Private 2.4 GB
#   （≈89–115 MB/格）。有回归样本时用回归值，没有时用这一档。
_ENTRY_SLOT_MEMORY_FALLBACK_BYTES = 110 * 1024 * 1024
_ENTRY_SLOT_MEMORY_MIN_BYTES = 8 * 1024 * 1024
_ENTRY_SLOT_MEMORY_MAX_BYTES = 2 * 1024 * 1024 * 1024
_ENTRY_MEMORY_SAMPLE_WINDOW_SECONDS = 600.0
_ENTRY_MEMORY_MIN_SPAN_SLOTS = 2


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
    ) -> None:
        self._controller = controller
        self._store = store
        self._system_metrics_sampler = system_metrics_sampler
        # 上游限流观测（回合闸的第四条轴）：由 runtime_service 注入模型负载均衡器的
        # 聚合读数，取不到就当没有这条约束。回合闸不许为了它去新起一套 429 计数。
        self._rate_limit_observer = rate_limit_observer
        self._process_handle: Any = None
        self._entry_memory_samples: deque[tuple[float, int, int]] = deque()
        self._entry_slot_memory_bytes = _ENTRY_SLOT_MEMORY_FALLBACK_BYTES
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
            'entry_gate_running_total': 0,
            'entry_slot_memory_bytes': int(_ENTRY_SLOT_MEMORY_FALLBACK_BYTES),
            'worker_memory_bytes': -1,
            'machine_memory_total_bytes': -1,
            'machine_memory_available_bytes': -1,
            'model_rate_penalty_429_max': 0.0,
            'model_rolling_rpm_60s_max': 0.0,
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

    @staticmethod
    def _entry_rate_ratio(penalty: float) -> float:
        """上游限流轴：安静时不设约束，越过实测 p90 只"保持"，越过 p99 才开始缓减。"""
        value = max(0.0, float(penalty or 0.0))
        if value >= _ENTRY_RATE_STEP_DOWN_PENALTY:
            return _ENTRY_AXIS_FLOOR_RATIO
        if value >= _ENTRY_RATE_HOLD_PENALTY:
            return 1.0
        return _ENTRY_AXIS_CEILING_RATIO

    def _update_entry_slot_memory(
        self,
        *,
        current_mono: float,
        running_slots: int,
        worker_memory_bytes: Any,
    ) -> None:
        """把滑动窗口里的 (在飞格数, worker RSS) 拟成一格内存成本。

        样本跨度不足或斜率出格就保留旧值。RSS 也会因缓存与工件而上涨，所以这个系数只会
        偏向高估成本（少放人），不会反过来把闸门抬高。
        """
        if worker_memory_bytes is None:
            return
        try:
            rss = int(worker_memory_bytes)
        except (TypeError, ValueError):
            return
        if rss <= 0:
            return
        samples = self._entry_memory_samples
        samples.append((float(current_mono), max(0, int(running_slots)), rss))
        cutoff = float(current_mono) - _ENTRY_MEMORY_SAMPLE_WINDOW_SECONDS
        while len(samples) > 1 and samples[0][0] < cutoff:
            samples.popleft()
        low = min(samples, key=lambda item: item[1])
        high = max(samples, key=lambda item: item[1])
        slot_span = high[1] - low[1]
        if slot_span < _ENTRY_MEMORY_MIN_SPAN_SLOTS:
            return
        estimated = int((high[2] - low[2]) // slot_span)
        if estimated < _ENTRY_SLOT_MEMORY_MIN_BYTES or estimated > _ENTRY_SLOT_MEMORY_MAX_BYTES:
            return
        self._entry_slot_memory_bytes = estimated

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

    def _compute_entry_targets(
        self,
        *,
        controller_snapshot: dict[str, Any],
        event_loop_lag_ms: float,
        writer_queue_depth: int,
        sqlite_write_wait_ms: float,
        sqlite_query_latency_ms: float,
        rate_pressure: dict[str, Any] | None,
        machine_memory_total_bytes: Any,
        machine_memory_available_bytes: Any,
        running_slots: int,
    ) -> dict[str, int]:
        """按角色算这一拍的回合闸目标。

        四条"自家积压"轴（事件循环 lag、写入队列、SQLite 写等、SQLite 读延迟）与上游限流
        轴取最小比例——它们都直接由本进程的并发造成，因此允许把闸位压到地板以下（最低 1）。
        内存余量只限制增长、不下压地板：机器内存吃紧多半是外部进程造成的，那该由磁盘/内存
        紧急道和处理，不该由操作员配置的地板替它背锅。
        """
        backlog_ratio = min(
            self._entry_axis_ratio(self._event_loop_warn_ms, event_loop_lag_ms),
            self._entry_axis_ratio(float(self._writer_queue_warn), writer_queue_depth),
            self._entry_axis_ratio(self._sqlite_write_wait_warn_ms, sqlite_write_wait_ms),
            self._entry_axis_ratio(self._sqlite_query_warn_ms, sqlite_query_latency_ms),
        )
        rate_ratio = self._entry_rate_ratio(dict(rate_pressure or {}).get('penalty_429_max') or 0.0)
        ratio = min(backlog_ratio, rate_ratio)
        # 收缩只由"紧急"触发：越过 warn 线只保持当前节点数（操作员要的正是这个档位），
        # 越过 critical 线或 429 惩罚到 p99 档才开始每拍缓减一格。
        penalty = max(0.0, float(dict(rate_pressure or {}).get('penalty_429_max') or 0.0))
        critical = (
            float(event_loop_lag_ms or 0.0) >= self._event_loop_critical_ms
            or int(writer_queue_depth or 0) >= self._writer_queue_critical
            or float(sqlite_write_wait_ms or 0.0) >= self._sqlite_write_wait_critical_ms
            or float(sqlite_query_latency_ms or 0.0) >= self._sqlite_query_critical_ms
            or penalty >= _ENTRY_RATE_STEP_DOWN_PENALTY
        )
        limits = dict(controller_snapshot.get('entry_gate_limit') or {})
        ceilings = dict(controller_snapshot.get('entry_gate_ceiling') or {})
        memory_ceiling = self._entry_memory_slot_ceiling(
            machine_memory_total_bytes=machine_memory_total_bytes,
            machine_memory_available_bytes=machine_memory_available_bytes,
            running_slots=running_slots,
        )
        targets: dict[str, int] = {}
        running_by_role = dict(controller_snapshot.get('entry_gate_running') or {})
        queued_by_role = dict(controller_snapshot.get('entry_gate_queued') or {})
        # 收缩只由"紧急"授权：越过 warn 线时把比例夹到 1.0 ⇒ 目标＝当前 ⇒ 保持现有节点数；
        # 越过 critical 线（或 429 惩罚到 p99 档）才让比例真正小于 1，逐拍下穿地板。
        # 不加这层夹：warn 级 lag（300ms 对 250ms 线）会一路把闸位削到 1 并钉死——
        # 292 个节点在排队时"循环落后 0.3 秒"是常态，不是紧急，削到 1 是塌方而不是控制。
        effective_ratio = ratio if critical else max(1.0, ratio)
        for role in sorted(set(limits) | set(ceilings)):
            normalized_role = str(role or '').strip().lower()
            if not normalized_role:
                continue
            ceiling = max(1, int(ceilings.get(normalized_role) or 1))
            current = max(1, int(limits.get(normalized_role) or ceiling))
            candidate = max(1, int(current * effective_ratio))
            # 增长只在闸真的卡住需求时发生：没人在闸口等、在飞的也没占满时抬闸位，
            # 得到的只是"随时可以一次放出几百份上下文物化"的空权限（21:31 无闸事故的形态）。
            queued = int(queued_by_role.get(normalized_role) or 0)
            running = int(running_by_role.get(normalized_role) or 0)
            binding = queued > 0 or running >= current
            if candidate > current:
                if not binding:
                    candidate = current
                elif memory_ceiling is None:
                    # 读不到内存读数时不许越过地板：v1 那版按机器水位放大，正是在
                    # "内存读数不可用/滞后"的状态下 90 秒爬到 27 格、把 Private 推到 2.4 GB。
                    candidate = ceiling
                else:
                    candidate = max(ceiling, min(candidate, memory_ceiling))
            targets[normalized_role] = candidate
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
            entry_targets = self._compute_entry_targets(
                controller_snapshot=controller_snapshot,
                event_loop_lag_ms=event_loop_lag_ms,
                writer_queue_depth=writer_queue_depth,
                sqlite_write_wait_ms=sqlite_write_wait_ms,
                sqlite_query_latency_ms=sqlite_query_latency_ms,
                rate_pressure=rate_pressure,
                machine_memory_total_bytes=machine_memory_total_bytes,
                machine_memory_available_bytes=machine_memory_available_bytes,
                running_slots=entry_running_total,
            )
            entry_limits = self._controller.set_entry_targets(entry_targets)
            self._snapshot['entry_gate_targets'] = dict(entry_targets)
            self._snapshot['entry_gate_limits'] = dict(entry_limits)
            self._snapshot['entry_gate_running_total'] = int(entry_running_total)
            self._snapshot['entry_slot_memory_bytes'] = int(self._entry_slot_memory_bytes)
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
            if lag_sampler is not None:
                lag_sampler.ping()
            event_loop_lag_ms = lag_sampler.sample(current_wall) if lag_sampler is not None else 0.0
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
