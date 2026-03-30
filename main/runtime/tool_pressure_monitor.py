from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from typing import Any, Callable

try:  # pragma: no cover - optional dependency in local dev before reinstall
    import psutil
except Exception:  # pragma: no cover - handled by runtime fallback
    psutil = None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


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
        recover_window_seconds: float = 10.0,
        warn_consecutive_samples: int = 3,
        safe_consecutive_samples: int = 5,
        pressure_snapshot_stale_after_seconds: float = 3.0,
        event_loop_warn_ms: float = 250.0,
        event_loop_safe_ms: float = 100.0,
        writer_queue_warn: int = 50,
        writer_queue_safe: int = 10,
        sqlite_write_wait_warn_ms: float = 200.0,
        sqlite_write_wait_safe_ms: float = 50.0,
        sqlite_query_warn_ms: float = 150.0,
        sqlite_query_safe_ms: float = 30.0,
        machine_cpu_warn_percent: float = 85.0,
        machine_cpu_safe_percent: float = 55.0,
        machine_memory_warn_percent: float = 88.0,
        machine_memory_safe_percent: float = 75.0,
        machine_disk_busy_warn_percent: float = 70.0,
        machine_disk_busy_safe_percent: float = 35.0,
        process_cpu_warn_ratio: float = 0.85,
        process_cpu_safe_ratio: float = 0.50,
        system_metrics_sampler: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._controller = controller
        self._store = store
        self._system_metrics_sampler = system_metrics_sampler
        self._lock = threading.RLock()
        self._sample_seconds = max(0.1, float(sample_seconds or 1.0))
        self._recover_window_seconds = max(0.1, float(recover_window_seconds or 10.0))
        self._warn_consecutive_samples = max(1, int(warn_consecutive_samples or 1))
        self._safe_consecutive_samples = max(1, int(safe_consecutive_samples or 1))
        self._pressure_snapshot_stale_after_seconds = max(0.1, float(pressure_snapshot_stale_after_seconds or 3.0))
        self._event_loop_warn_ms = max(0.0, float(event_loop_warn_ms or 0.0))
        self._event_loop_safe_ms = max(0.0, float(event_loop_safe_ms or 0.0))
        self._writer_queue_warn = max(0, int(writer_queue_warn or 0))
        self._writer_queue_safe = max(0, int(writer_queue_safe or 0))
        self._sqlite_write_wait_warn_ms = max(0.0, float(sqlite_write_wait_warn_ms or 0.0))
        self._sqlite_write_wait_safe_ms = max(0.0, float(sqlite_write_wait_safe_ms or 0.0))
        self._sqlite_query_warn_ms = max(0.0, float(sqlite_query_warn_ms or 0.0))
        self._sqlite_query_safe_ms = max(0.0, float(sqlite_query_safe_ms or 0.0))
        self._machine_cpu_warn_percent = max(0.0, float(machine_cpu_warn_percent or 0.0))
        self._machine_cpu_safe_percent = max(0.0, float(machine_cpu_safe_percent or 0.0))
        self._machine_memory_warn_percent = max(0.0, float(machine_memory_warn_percent or 0.0))
        self._machine_memory_safe_percent = max(0.0, float(machine_memory_safe_percent or 0.0))
        self._machine_disk_busy_warn_percent = max(0.0, float(machine_disk_busy_warn_percent or 0.0))
        self._machine_disk_busy_safe_percent = max(0.0, float(machine_disk_busy_safe_percent or 0.0))
        self._process_cpu_warn_ratio = max(0.0, float(process_cpu_warn_ratio or 0.0))
        self._process_cpu_safe_ratio = max(0.0, float(process_cpu_safe_ratio or 0.0))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lag_sampler: _EventLoopLagSampler | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._consecutive_warn = 0
        self._consecutive_safe = 0
        self._last_recovery_step_at = 0.0
        self._sample_mono = 0.0
        self._last_disk_sample: Any = None
        self._last_disk_sample_mono = 0.0
        self._snapshot: dict[str, Any] = {
            'machine_pressure_available': False,
            'machine_pressure_cpu_percent': 0.0,
            'machine_pressure_memory_percent': 0.0,
            'machine_pressure_disk_busy_percent': 0.0,
            'machine_pressure_disk_busy_available': False,
            'machine_pressure_disk_read_bytes_per_sec': 0.0,
            'machine_pressure_disk_write_bytes_per_sec': 0.0,
            'tool_pressure_event_loop_lag_ms': 0.0,
            'tool_pressure_writer_queue_depth': 0,
            'tool_pressure_process_cpu_ratio': 0.0,
            'sqlite_write_wait_ms': 0.0,
            'sqlite_query_latency_ms': 0.0,
            'pressure_sample_at': '',
            'tool_pressure_sample_at': '',
        }

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
        writer_queue_warn: int,
        writer_queue_safe: int,
        sqlite_write_wait_warn_ms: float,
        sqlite_write_wait_safe_ms: float,
        sqlite_query_warn_ms: float,
        sqlite_query_safe_ms: float,
        machine_cpu_warn_percent: float,
        machine_cpu_safe_percent: float,
        machine_memory_warn_percent: float,
        machine_memory_safe_percent: float,
        machine_disk_busy_warn_percent: float,
        machine_disk_busy_safe_percent: float,
        process_cpu_warn_ratio: float,
        process_cpu_safe_ratio: float,
    ) -> None:
        with self._lock:
            self._sample_seconds = max(0.1, float(sample_seconds or 1.0))
            self._recover_window_seconds = max(0.1, float(recover_window_seconds or 10.0))
            self._warn_consecutive_samples = max(1, int(warn_consecutive_samples or 1))
            self._safe_consecutive_samples = max(1, int(safe_consecutive_samples or 1))
            self._pressure_snapshot_stale_after_seconds = max(0.1, float(pressure_snapshot_stale_after_seconds or 3.0))
            self._event_loop_warn_ms = max(0.0, float(event_loop_warn_ms or 0.0))
            self._event_loop_safe_ms = max(0.0, float(event_loop_safe_ms or 0.0))
            self._writer_queue_warn = max(0, int(writer_queue_warn or 0))
            self._writer_queue_safe = max(0, int(writer_queue_safe or 0))
            self._sqlite_write_wait_warn_ms = max(0.0, float(sqlite_write_wait_warn_ms or 0.0))
            self._sqlite_write_wait_safe_ms = max(0.0, float(sqlite_write_wait_safe_ms or 0.0))
            self._sqlite_query_warn_ms = max(0.0, float(sqlite_query_warn_ms or 0.0))
            self._sqlite_query_safe_ms = max(0.0, float(sqlite_query_safe_ms or 0.0))
            self._machine_cpu_warn_percent = max(0.0, float(machine_cpu_warn_percent or 0.0))
            self._machine_cpu_safe_percent = max(0.0, float(machine_cpu_safe_percent or 0.0))
            self._machine_memory_warn_percent = max(0.0, float(machine_memory_warn_percent or 0.0))
            self._machine_memory_safe_percent = max(0.0, float(machine_memory_safe_percent or 0.0))
            self._machine_disk_busy_warn_percent = max(0.0, float(machine_disk_busy_warn_percent or 0.0))
            self._machine_disk_busy_safe_percent = max(0.0, float(machine_disk_busy_safe_percent or 0.0))
            self._process_cpu_warn_ratio = max(0.0, float(process_cpu_warn_ratio or 0.0))
            self._process_cpu_safe_ratio = max(0.0, float(process_cpu_safe_ratio or 0.0))

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
    ) -> dict[str, Any]:
        current_mono = float(now_mono if now_mono is not None else time.perf_counter())
        timestamp = str(now_iso or _now_iso()).strip() or _now_iso()
        machine_missing = not bool(machine_available)
        machine_warn = (
            float(machine_cpu_percent or 0.0) >= self._machine_cpu_warn_percent
            or float(machine_memory_percent or 0.0) >= self._machine_memory_warn_percent
            or (bool(disk_busy_available) and float(machine_disk_busy_percent or 0.0) >= self._machine_disk_busy_warn_percent)
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
        local_warn = (
            float(event_loop_lag_ms or 0.0) >= self._event_loop_warn_ms
            or int(writer_queue_depth or 0) >= self._writer_queue_warn
            or float(sqlite_write_wait_ms or 0.0) >= self._sqlite_write_wait_warn_ms
            or float(sqlite_query_latency_ms or 0.0) >= self._sqlite_query_warn_ms
            or float(process_cpu_ratio or 0.0) >= self._process_cpu_warn_ratio
        )
        local_safe = (
            float(event_loop_lag_ms or 0.0) <= self._event_loop_safe_ms
            and int(writer_queue_depth or 0) <= self._writer_queue_safe
            and float(sqlite_write_wait_ms or 0.0) <= self._sqlite_write_wait_safe_ms
            and float(sqlite_query_latency_ms or 0.0) <= self._sqlite_query_safe_ms
            and float(process_cpu_ratio or 0.0) <= self._process_cpu_safe_ratio
        )
        warn = machine_missing or machine_warn or local_warn
        safe = machine_safe and local_safe
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
                'tool_pressure_event_loop_lag_ms': round(max(0.0, float(event_loop_lag_ms or 0.0)), 3),
                'tool_pressure_writer_queue_depth': int(max(0, int(writer_queue_depth or 0))),
                'tool_pressure_process_cpu_ratio': round(max(0.0, float(process_cpu_ratio or 0.0)), 4),
                'sqlite_write_wait_ms': round(max(0.0, float(sqlite_write_wait_ms or 0.0)), 3),
                'sqlite_query_latency_ms': round(max(0.0, float(sqlite_query_latency_ms or 0.0)), 3),
                'pressure_sample_at': timestamp,
                'tool_pressure_sample_at': timestamp,
            }
            if warn:
                self._consecutive_warn += 1
                self._consecutive_safe = 0
            elif safe:
                self._consecutive_safe += 1
                self._consecutive_warn = 0
            else:
                self._consecutive_warn = 0
                self._consecutive_safe = 0

            current_state = str(self._controller.snapshot().get('tool_pressure_state') or 'normal')
            if self._consecutive_warn >= self._warn_consecutive_samples:
                self._controller.throttle(at=timestamp)
                self._last_recovery_step_at = current_mono
            elif current_state == 'throttled' and self._consecutive_safe >= self._safe_consecutive_samples:
                self._controller.begin_recovery(at=timestamp)
                self._last_recovery_step_at = current_mono
            elif current_state == 'recovering':
                if current_mono - self._last_recovery_step_at >= self._recover_window_seconds and self._consecutive_safe >= self._safe_consecutive_samples:
                    if self._controller.step_recovery(at=timestamp):
                        self._last_recovery_step_at = current_mono
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
                )
            except Exception:
                time.sleep(min(1.0, self._sample_seconds))
            last_wall = current_wall
            last_cpu = current_cpu

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
            current_busy_time = getattr(disk, 'busy_time', None)
            previous_busy_time = getattr(self._last_disk_sample, 'busy_time', None)
            if current_busy_time is not None and previous_busy_time is not None:
                disk_busy_available = True
                disk_busy_percent = min(
                    100.0,
                    max(0.0, float(current_busy_time - previous_busy_time) / (wall_delta * 1000.0) * 100.0),
                )
        self._last_disk_sample = disk
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
