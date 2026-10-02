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
    """最近若干条长块 + 本进程生命周期内最长的若干条。

    只留"最近 N 条"那一本不够用：实盘噪声块（`get_task_snapshot` 210–543 ms）以每秒级
    频率把稀有块冲掉——lag 采到 8,169 ms 的那一分钟，榜上最高只有 543 ms，秒级归因就此
    断线。worst 榜按耗时留位，被更长的块才挤掉，`started_at` 带着它发生在哪一刻。
    """

    def __init__(
        self,
        *,
        max_entries: int = 8,
        threshold_ms: float = 200.0,
        worst_entries: int = 8,
    ) -> None:
        self._lock = threading.RLock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_entries or 1)))
        self._worst: list[dict[str, Any]] = []
        self._worst_max = max(1, int(worst_entries or 1))
        self._threshold_ms = max(1.0, float(threshold_ms or 200.0))

    def record(self, *, section: str, elapsed_ms: float, started_at: str | None = None) -> None:
        duration = max(0.0, float(elapsed_ms or 0.0))
        if duration < self._threshold_ms:
            return
        entry = {
            'section': str(section or 'unknown').strip() or 'unknown',
            'elapsed_ms': round(duration, 3),
            'started_at': str(started_at or _now_iso()).strip() or _now_iso(),
        }
        with self._lock:
            self._entries.append(dict(entry))
            if len(self._worst) >= self._worst_max and duration <= min(
                float(item['elapsed_ms']) for item in self._worst
            ):
                return
            self._worst.append(entry)
            self._worst.sort(key=lambda item: float(item['elapsed_ms']), reverse=True)
            del self._worst[self._worst_max:]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._entries]

    def worst_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._worst]

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
