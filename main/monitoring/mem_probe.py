"""分配驻留探针：把进程内 Python 分配按站点记成 JSONL，供恢复风暴这类峰值归因使用。

只有进程内 tracemalloc 能回答"某一刻同时驻留的那几百 MB 是谁分配的"——py-spy 给的是
CPU 热点，Get-Process 给的是总量。开关是数据根里的一个标记文件（与 auto-unlock.key 同
一族）：web 托管的 worker 由 `os.environ.copy()` 继承父进程环境，环境变量闸门要改它就得
重启 web，而标记文件只要重启 worker。
"""

from __future__ import annotations

import json
import os
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

try:  # pragma: no cover - optional dependency in local dev before reinstall
    import psutil
except Exception:  # pragma: no cover - handled by runtime fallback
    psutil = None

MARKER_NAME = 'mem-probe.on'
OUTPUT_DIR_NAME = 'mem-probe'
DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_TOP_LIMIT = 12
_NFRAMES = 1


def mem_probe_marker(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / MARKER_NAME


def mem_probe_requested(marker_path: Path) -> bool:
    return Path(marker_path).exists()


def _interval_seconds() -> float:
    raw = str(os.environ.get('G3KU_MEM_PROBE_INTERVAL_SECONDS') or '').strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value >= 1 else DEFAULT_INTERVAL_SECONDS


def _top_limit() -> int:
    raw = str(os.environ.get('G3KU_MEM_PROBE_TOP_LIMIT') or '').strip()
    if not raw:
        return DEFAULT_TOP_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TOP_LIMIT
    return value if value >= 1 else DEFAULT_TOP_LIMIT


def _process_memory_mb() -> tuple[float, float]:
    if psutil is None:
        return 0.0, 0.0
    try:
        info = psutil.Process().memory_info()
        return round(float(info.rss) / 1048576.0, 1), round(float(info.private) / 1048576.0, 1)
    except Exception:
        return 0.0, 0.0


def _top_entries(stats) -> list[dict[str, object]]:
    entries = []
    for stat in stats:
        site = str(stat.traceback[0].filename) + ':' + str(stat.traceback[0].lineno)
        entries.append(
            {
                'site': site,
                'mb': round(float(stat.size) / 1048576.0, 2),
                'blocks': int(stat.count),
            }
        )
    return entries


class MemProbe:
    """按固定间隔落一行 JSONL；失败只停自己，绝不影响被观测的进程。"""

    def __init__(self, *, output_path: Path, interval_seconds: float, top_limit: int) -> None:
        self._output_path = Path(output_path)
        self._interval_seconds = float(interval_seconds)
        self._top_limit = int(top_limit)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_snapshot = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        if not tracemalloc.is_tracing():
            tracemalloc.start(_NFRAMES)
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._thread_main,
            name='mem-probe',
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=5.0)

    def sample_once(self) -> dict[str, object] | None:
        snapshot = tracemalloc.take_snapshot()
        rss_mb, private_mb = _process_memory_mb()
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        previous = self._previous_snapshot
        self._previous_snapshot = snapshot
        growth = snapshot.compare_to(previous, 'lineno')[: self._top_limit] if previous is not None else []
        row: dict[str, object] = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'pid': os.getpid(),
            'rss_mb': rss_mb,
            'private_mb': private_mb,
            'traced_mb': round(traced_current / 1048576.0, 2),
            'traced_peak_mb': round(traced_peak / 1048576.0, 2),
            'growth_mb': 0.0,
            'top': _top_entries(snapshot.statistics('lineno')[: self._top_limit]),
            'growth': _top_entries(growth),
        }
        if previous is not None:
            previous_total = sum(stat.size for stat in previous.statistics('total'))
            row['growth_mb'] = round((traced_current - previous_total) / 1048576.0, 2)
        return row

    def _thread_main(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                row = self.sample_once()
            except Exception:
                return
            if row is None:
                continue
            try:
                with self._output_path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            except Exception:
                return


def start_mem_probe(*, runtime_dir: Path) -> MemProbe | None:
    """标记文件在则启动探针，输出到 `<runtime_dir>/mem-probe/mem-probe-<pid>.jsonl`。"""

    marker = mem_probe_marker(runtime_dir)
    if not mem_probe_requested(marker):
        return None
    stamp = time.strftime('%Y%m%d-%H%M%S')
    output_path = Path(runtime_dir) / OUTPUT_DIR_NAME / f'mem-probe-{os.getpid()}-{stamp}.jsonl'
    probe = MemProbe(
        output_path=output_path,
        interval_seconds=_interval_seconds(),
        top_limit=_top_limit(),
    )
    probe.start()
    return probe
