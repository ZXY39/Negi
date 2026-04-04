from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


class RuntimeDebugRecorder:
    def __init__(self, *, max_entries: int = 8, threshold_ms: float = 200.0) -> None:
        self._lock = threading.RLock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_entries or 1)))
        self._threshold_ms = max(1.0, float(threshold_ms or 200.0))

    def record(self, *, section: str, elapsed_ms: float, started_at: str | None = None) -> None:
        duration = max(0.0, float(elapsed_ms or 0.0))
        if duration < self._threshold_ms:
            return
        with self._lock:
            self._entries.append(
                {
                    'section': str(section or 'unknown').strip() or 'unknown',
                    'elapsed_ms': round(duration, 3),
                    'started_at': str(started_at or _now_iso()).strip() or _now_iso(),
                }
            )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._entries]

    @contextmanager
    def track(self, section: str) -> Iterator[None]:
        started_at = _now_iso()
        started_mono = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                section=section,
                elapsed_ms=(time.perf_counter() - started_mono) * 1000.0,
                started_at=started_at,
            )
