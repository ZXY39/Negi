from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from loguru import logger

from main.protocol import now_iso

# 存活日志间隔（秒）：worker 静默期的心跳日志金丝雀——文件超过该间隔的
# 数倍时长仍无新行而 worker_leases 心跳新鲜，指向日志输出层而非进程死亡。
_WORKER_ALIVE_LOG_INTERVAL_SECONDS = 600.0

# 性能历史采样：worker_status 是每 worker 单行的覆盖快照，只答得出"现在"，
# 答不出"停滞那段时间机器压不压"。这里按固定节拍把性能条用到的字段追加成
# perf_samples 行，供 perf_inspect 工具与失速事件回看历史窗口。
# 15s 而不是 1s：判停滞只需要知道某段窗口是否处于收紧/排队，1s 粒度对结论
# 无增益却把行数与库体积乘 15。
_PERF_HISTORY_INTERVAL_SECONDS = 15.0
_PERF_HISTORY_RETENTION_SECONDS = 24 * 60 * 60
# 每写入 N 行顺带做一次过期裁剪（≈ 每小时一次扫描），不另起定时器。
_PERF_HISTORY_PRUNE_EVERY_SAMPLES = 240
# 字段名与 worker-status 载荷同一份词表，reader 侧不再翻译一次。
_PERF_HISTORY_FIELDS = (
    'budget_state',
    'machine_pressure_state',
    'local_pressure_state',
    'machine_pressure_available',
    'machine_pressure_cpu_percent',
    'machine_pressure_memory_percent',
    'machine_pressure_disk_busy_percent',
    'machine_pressure_disk_busy_available',
    'machine_disk_free_bytes',
    'disk_emergency_active',
    'tool_queue_running_count',
    'tool_queue_waiting_count',
    'node_queue_running_count',
    'node_queue_waiting_count',
    'node_queue_frozen_count',
    'node_queue_oldest_wait_ms',
    'worker_execution_oldest_wait_ms',
    'tool_pressure_event_loop_lag_ms',
    'tool_pressure_writer_queue_depth',
    'sqlite_write_wait_ms',
    'sqlite_query_latency_ms',
    'active_task_count',
)


class WorkerHeartbeatServiceV2:
    def __init__(
        self,
        *,
        store,
        scheduler,
        execution_mode: str,
        worker_id: str,
        publish_status: Callable[[dict[str, Any]], None],
        pressure_snapshot_supplier: Callable[[], dict[str, Any]] | None = None,
        debug_snapshot_supplier: Callable[[], dict[str, Any]] | None = None,
        lease_heartbeat: Callable[[str, dict[str, Any]], None] | None = None,
        alive_log_interval_seconds: float = _WORKER_ALIVE_LOG_INTERVAL_SECONDS,
        perf_history_interval_seconds: float = _PERF_HISTORY_INTERVAL_SECONDS,
        perf_history_retention_seconds: float = _PERF_HISTORY_RETENTION_SECONDS,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._execution_mode = str(execution_mode or '').strip()
        self._worker_id = str(worker_id or '').strip() or 'worker'
        self._publish_status = publish_status
        self._pressure_snapshot_supplier = pressure_snapshot_supplier if callable(pressure_snapshot_supplier) else None
        self._debug_snapshot_supplier = debug_snapshot_supplier if callable(debug_snapshot_supplier) else None
        self._lease_heartbeat = lease_heartbeat if callable(lease_heartbeat) else None
        self._alive_log_interval_seconds = max(0.0, float(alive_log_interval_seconds or 0.0))
        self._perf_history_interval_seconds = max(0.0, float(perf_history_interval_seconds or 0.0))
        self._perf_history_retention_seconds = max(0.0, float(perf_history_retention_seconds or 0.0))
        self._last_perf_sample_mono = 0.0
        self._perf_samples_written = 0
        self._beat_failures = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start_background(self) -> None:
        self._start_thread()

    async def run_forever(self) -> None:
        self._start_thread()
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await self.close()
            raise

    async def close(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        self._stop_event.set()
        await asyncio.to_thread(thread.join, 5.0)

    def _start_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f'task-worker-heartbeat:{self._worker_id}',
            daemon=True,
        )
        self._thread.start()

    def _thread_main(self) -> None:
        beats = 0
        last_alive_log_mono = 0.0
        while not self._stop_event.is_set():
            try:
                active_task_count = self._scheduler.active_task_count() + self._scheduler.queued_task_count()
                updated_at = now_iso()
                pressure_snapshot = dict(self._pressure_snapshot_supplier() or {}) if self._pressure_snapshot_supplier is not None else {}
                debug_snapshot = dict(self._debug_snapshot_supplier() or {}) if self._debug_snapshot_supplier is not None else {}
                payload = {
                    'execution_mode': self._execution_mode,
                    'active_task_count': active_task_count,
                    'worker_heartbeat_at': updated_at,
                    **pressure_snapshot,
                }
                if debug_snapshot:
                    payload['debug'] = debug_snapshot
                item = {
                    'worker_id': self._worker_id,
                    'role': 'task_worker',
                    'status': 'running',
                    'updated_at': updated_at,
                    'payload': payload,
                }
                self._store.upsert_worker_status(
                    worker_id=self._worker_id,
                    role='task_worker',
                    status='running',
                    updated_at=updated_at,
                    payload=payload,
                )
                # 性能历史紧跟状态落库：这两步写的是同一份 payload，而下游的
                # lease 续期 / WS 桥接投递任何一步抛异常都会让本拍提前结束。
                # 采样器一旦排在它们后面就会被无关故障长期连坐（实盘出现过 0 行）。
                self._record_perf_history(payload, updated_at)
                if self._lease_heartbeat is not None:
                    self._lease_heartbeat(updated_at, payload)
                self._publish_status(item)
                beats += 1
                self._beat_failures = 0
                # 存活金丝雀：worker 静默期按间隔补一行心跳日志（web 侧不emit，
                # console.log 自有持续流量）。附带库层写失败计数，非零时一并对账。
                if (
                    self._execution_mode == 'worker'
                    and self._alive_log_interval_seconds > 0
                    and (time.monotonic() - last_alive_log_mono) >= self._alive_log_interval_seconds
                ):
                    last_alive_log_mono = time.monotonic()
                    write_failures: dict[str, Any] = {}
                    failure_counter = getattr(self._store, 'write_failure_counts', None)
                    if callable(failure_counter):
                        try:
                            write_failures = dict(failure_counter() or {})
                        except Exception:
                            write_failures = {}
                    logger.info(
                        'worker heartbeat alive: worker_id={} pid={} active_tasks={} beats={} sqlite_write_failures={}',
                        self._worker_id,
                        os.getpid(),
                        active_task_count,
                        beats,
                        write_failures or '-',
                    )
                wait_seconds = 1.0 if active_task_count > 0 else 2.0
                self._stop_event.wait(wait_seconds)
            except Exception as exc:
                # 节拍抛异常过去是彻底静默的：worker_status 照常新鲜，下游
                # （金丝雀、租约、状态桥接）却长期不执行——排查看不到任何线索。
                # 首发与之后每 60 拍报一条，最坏约每分钟一行，不刷屏也不装死。
                self._beat_failures += 1
                if self._beat_failures == 1 or self._beat_failures % 60 == 0:
                    logger.warning(
                        'worker heartbeat beat failed ({} consecutive): {}: {}',
                        self._beat_failures,
                        type(exc).__name__,
                        exc,
                    )
                self._stop_event.wait(1.0)

    def _record_perf_history(self, payload: dict[str, Any], sampled_at: str) -> None:
        # 历史是辅助排障数据：本方法自己吞掉存储异常，绝不吃掉 worker_status 落库
        # 与后续控制流；写入位置紧跟状态落库，见调用点注释。
        if self._perf_history_interval_seconds <= 0.0:
            return
        current_mono = time.monotonic()
        if (current_mono - self._last_perf_sample_mono) < self._perf_history_interval_seconds:
            return
        self._last_perf_sample_mono = current_mono
        row = {key: payload.get(key) for key in _PERF_HISTORY_FIELDS if key in payload}
        if not row:
            return
        try:
            self._store.record_perf_sample(
                sampled_at=sampled_at,
                worker_id=self._worker_id,
                payload=row,
            )
        except Exception:
            logger.exception('failed to record perf sample for {}', self._worker_id)
            return
        self._perf_samples_written += 1
        if self._perf_history_retention_seconds <= 0.0:
            return
        if self._perf_samples_written % _PERF_HISTORY_PRUNE_EVERY_SAMPLES:
            return
        cutoff = (datetime.now().astimezone() - timedelta(seconds=self._perf_history_retention_seconds))
        try:
            self._store.prune_perf_samples(before_iso=cutoff.isoformat(timespec='seconds'))
        except Exception:
            logger.exception('failed to prune perf samples for {}', self._worker_id)
