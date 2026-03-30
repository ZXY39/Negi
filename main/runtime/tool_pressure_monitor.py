from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


class ToolPressureMonitor:
    def __init__(
        self,
        *,
        controller,
        store,
        sample_seconds: float = 1.0,
        recover_window_seconds: float = 10.0,
        warn_consecutive_samples: int = 3,
        safe_consecutive_samples: int = 5,
        event_loop_warn_ms: float = 250.0,
        event_loop_safe_ms: float = 100.0,
        writer_queue_warn: int = 50,
        writer_queue_safe: int = 10,
        process_cpu_warn_ratio: float = 0.85,
        process_cpu_safe_ratio: float = 0.50,
    ) -> None:
        self._controller = controller
        self._store = store
        self._lock = threading.RLock()
        self._sample_seconds = max(0.1, float(sample_seconds or 1.0))
        self._recover_window_seconds = max(0.1, float(recover_window_seconds or 10.0))
        self._warn_consecutive_samples = max(1, int(warn_consecutive_samples or 1))
        self._safe_consecutive_samples = max(1, int(safe_consecutive_samples or 1))
        self._event_loop_warn_ms = max(0.0, float(event_loop_warn_ms or 0.0))
        self._event_loop_safe_ms = max(0.0, float(event_loop_safe_ms or 0.0))
        self._writer_queue_warn = max(0, int(writer_queue_warn or 0))
        self._writer_queue_safe = max(0, int(writer_queue_safe or 0))
        self._process_cpu_warn_ratio = max(0.0, float(process_cpu_warn_ratio or 0.0))
        self._process_cpu_safe_ratio = max(0.0, float(process_cpu_safe_ratio or 0.0))
        self._loop_task: asyncio.Task[None] | None = None
        self._consecutive_warn = 0
        self._consecutive_safe = 0
        self._last_recovery_step_at = 0.0
        self._snapshot: dict[str, Any] = {
            'tool_pressure_event_loop_lag_ms': 0.0,
            'tool_pressure_writer_queue_depth': 0,
            'tool_pressure_process_cpu_ratio': 0.0,
        }

    def configure(
        self,
        *,
        sample_seconds: float,
        recover_window_seconds: float,
        warn_consecutive_samples: int,
        safe_consecutive_samples: int,
        event_loop_warn_ms: float,
        event_loop_safe_ms: float,
        writer_queue_warn: int,
        writer_queue_safe: int,
        process_cpu_warn_ratio: float,
        process_cpu_safe_ratio: float,
    ) -> None:
        with self._lock:
            self._sample_seconds = max(0.1, float(sample_seconds or 1.0))
            self._recover_window_seconds = max(0.1, float(recover_window_seconds or 10.0))
            self._warn_consecutive_samples = max(1, int(warn_consecutive_samples or 1))
            self._safe_consecutive_samples = max(1, int(safe_consecutive_samples or 1))
            self._event_loop_warn_ms = max(0.0, float(event_loop_warn_ms or 0.0))
            self._event_loop_safe_ms = max(0.0, float(event_loop_safe_ms or 0.0))
            self._writer_queue_warn = max(0, int(writer_queue_warn or 0))
            self._writer_queue_safe = max(0, int(writer_queue_safe or 0))
            self._process_cpu_warn_ratio = max(0.0, float(process_cpu_warn_ratio or 0.0))
            self._process_cpu_safe_ratio = max(0.0, float(process_cpu_safe_ratio or 0.0))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._snapshot)
        payload.update(self._controller.snapshot())
        return payload

    def observe_sample(
        self,
        *,
        event_loop_lag_ms: float,
        writer_queue_depth: int,
        process_cpu_ratio: float,
        now_mono: float | None = None,
        now_iso: str | None = None,
    ) -> dict[str, Any]:
        current_mono = float(now_mono if now_mono is not None else time.perf_counter())
        timestamp = str(now_iso or _now_iso()).strip() or _now_iso()
        warn = (
            float(event_loop_lag_ms or 0.0) >= self._event_loop_warn_ms
            or int(writer_queue_depth or 0) >= self._writer_queue_warn
            or float(process_cpu_ratio or 0.0) >= self._process_cpu_warn_ratio
        )
        safe = (
            float(event_loop_lag_ms or 0.0) <= self._event_loop_safe_ms
            and int(writer_queue_depth or 0) <= self._writer_queue_safe
            and float(process_cpu_ratio or 0.0) <= self._process_cpu_safe_ratio
        )
        with self._lock:
            self._snapshot = {
                'tool_pressure_event_loop_lag_ms': round(max(0.0, float(event_loop_lag_ms or 0.0)), 3),
                'tool_pressure_writer_queue_depth': int(max(0, int(writer_queue_depth or 0))),
                'tool_pressure_process_cpu_ratio': round(max(0.0, float(process_cpu_ratio or 0.0)), 4),
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
        if self._loop_task is not None and not self._loop_task.done():
            return
        loop = asyncio.get_running_loop()
        self._loop_task = loop.create_task(self.run_forever(), name='task-tool-pressure-monitor')

    async def close(self) -> None:
        task = self._loop_task
        self._loop_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def run_forever(self) -> None:
        last_wall = time.perf_counter()
        last_cpu = time.process_time()
        while True:
            try:
                await asyncio.sleep(self._sample_seconds)
                current_wall = time.perf_counter()
                current_cpu = time.process_time()
                wall_delta = max(1e-6, current_wall - last_wall)
                cpu_delta = max(0.0, current_cpu - last_cpu)
                event_loop_lag_ms = max(0.0, (wall_delta - self._sample_seconds) * 1000.0)
                writer_queue_depth = int(getattr(self._store, 'writer_queue_depth', lambda: 0)() or 0)
                process_cpu_ratio = cpu_delta / wall_delta
                self.observe_sample(
                    event_loop_lag_ms=event_loop_lag_ms,
                    writer_queue_depth=writer_queue_depth,
                    process_cpu_ratio=process_cpu_ratio,
                    now_mono=current_wall,
                )
                last_wall = current_wall
                last_cpu = current_cpu
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)
