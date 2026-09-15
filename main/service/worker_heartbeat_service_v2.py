from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable

from loguru import logger

from main.protocol import now_iso

# 存活日志间隔（秒）：worker 静默期的心跳日志金丝雀——文件超过该间隔的
# 数倍时长仍无新行而 worker_leases 心跳新鲜，指向日志输出层而非进程死亡。
_WORKER_ALIVE_LOG_INTERVAL_SECONDS = 600.0


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
                if self._lease_heartbeat is not None:
                    self._lease_heartbeat(updated_at, payload)
                self._publish_status(item)
                beats += 1
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
            except Exception:
                self._stop_event.wait(1.0)
