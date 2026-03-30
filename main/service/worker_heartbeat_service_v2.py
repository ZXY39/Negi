from __future__ import annotations

import asyncio
from typing import Any, Callable

from main.protocol import now_iso


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
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._execution_mode = str(execution_mode or '').strip()
        self._worker_id = str(worker_id or '').strip() or 'worker'
        self._publish_status = publish_status
        self._pressure_snapshot_supplier = pressure_snapshot_supplier if callable(pressure_snapshot_supplier) else None

    async def run_forever(self) -> None:
        while True:
            try:
                active_task_count = self._scheduler.active_task_count() + self._scheduler.queued_task_count()
                updated_at = now_iso()
                pressure_snapshot = dict(self._pressure_snapshot_supplier() or {}) if self._pressure_snapshot_supplier is not None else {}
                payload = {
                    'execution_mode': self._execution_mode,
                    'active_task_count': active_task_count,
                    **pressure_snapshot,
                }
                self._store.upsert_worker_status(
                    worker_id=self._worker_id,
                    role='task_worker',
                    status='running',
                    updated_at=updated_at,
                    payload=payload,
                )
                self._publish_status(
                    {
                        'worker_id': self._worker_id,
                        'role': 'task_worker',
                        'status': 'running',
                        'updated_at': updated_at,
                        'payload': payload,
                    }
                )
                await asyncio.sleep(1.0 if active_task_count > 0 else 5.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)
