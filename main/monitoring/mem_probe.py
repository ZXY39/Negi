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

from loguru import logger

try:  # pragma: no cover - optional dependency in local dev before reinstall
    import psutil
except Exception:  # pragma: no cover - handled by runtime fallback
    psutil = None

MARKER_NAME = 'mem-probe.on'
OUTPUT_DIR_NAME = 'mem-probe'
DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_TOP_LIMIT = 12
DEFAULT_FRAMES = 1
DEFAULT_DUMP_LIMIT = 40


def _marker_settings(marker_path: Path) -> dict[str, str]:
    """标记文件正文按 `key=value` 读（逗号/换行分隔）。

    托管 worker 的环境来自 web 进程的 os.environ.copy()，调参光靠环境变量就得重启 web；
    写在标记里只要重启 worker。优先级：环境变量 > 标记 > 默认值。
    """
    settings: dict[str, str] = {}
    try:
        text = Path(marker_path).read_text(encoding='utf-8')
    except OSError:
        return settings
    for chunk in text.replace(',', '\n').splitlines():
        key, sep, value = chunk.partition('=')
        if sep:
            settings[key.strip().lower()] = value.strip()
    return settings


def _number(
    settings: dict[str, str],
    *,
    env_name: str,
    marker_key: str,
    default: float,
    cast: str = 'int',
    minimum: float,
) -> float:
    raw = str(os.environ.get(env_name) or '').strip() or settings.get(marker_key, '')
    if not raw:
        return default
    try:
        value = int(raw) if cast == 'int' else float(raw)
    except ValueError:
        return default
    return float(value) if value >= minimum else default


def mem_probe_marker(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / MARKER_NAME


def mem_probe_requested(marker_path: Path) -> bool:
    return Path(marker_path).exists()


def probe_settings(marker_path: Path) -> dict[str, float]:
    settings = _marker_settings(marker_path)
    return {
        'interval_seconds': _number(
            settings,
            env_name='G3KU_MEM_PROBE_INTERVAL_SECONDS',
            marker_key='interval',
            default=DEFAULT_INTERVAL_SECONDS,
            cast='float',
            minimum=1.0,
        ),
        'top_limit': _number(
            settings,
            env_name='G3KU_MEM_PROBE_TOP_LIMIT',
            marker_key='top',
            default=DEFAULT_TOP_LIMIT,
            minimum=1,
        ),
        'frames': _number(
            settings,
            env_name='G3KU_MEM_PROBE_FRAMES',
            marker_key='frames',
            default=DEFAULT_FRAMES,
            minimum=1,
        ),
        'dump_mb': _number(
            settings,
            env_name='G3KU_MEM_PROBE_DUMP_MB',
            marker_key='dump_mb',
            default=0,
            cast='float',
            minimum=0,
        ),
    }


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

    def __init__(
        self,
        *,
        output_path: Path,
        interval_seconds: float,
        top_limit: int,
        frames: int = DEFAULT_FRAMES,
        dump_threshold_bytes: int = 0,
        dump_limit: int = DEFAULT_DUMP_LIMIT,
    ) -> None:
        self._output_path = Path(output_path)
        self._interval_seconds = float(interval_seconds)
        self._top_limit = int(top_limit)
        self._frames = int(frames)
        self._dump_threshold_bytes = int(dump_threshold_bytes)
        self._dump_limit = int(dump_limit)
        self._dumped = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_snapshot = None
        self._last_snapshot = None
        self._previous_traced_bytes: int | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        if not tracemalloc.is_tracing():
            tracemalloc.start(self._frames)
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
        self._last_snapshot = snapshot
        rss_mb, private_mb = _process_memory_mb()
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        previous = self._previous_snapshot
        previous_total = self._previous_traced_bytes
        self._previous_snapshot = snapshot
        self._previous_traced_bytes = traced_current
        growth = snapshot.compare_to(previous, 'lineno')[: self._top_limit] if previous is not None else []
        row: dict[str, object] = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'pid': os.getpid(),
            'rss_mb': rss_mb,
            'private_mb': private_mb,
            'traced_mb': round(traced_current / 1048576.0, 2),
            'traced_peak_mb': round(traced_peak / 1048576.0, 2),
            'growth_mb': round((traced_current - previous_total) / 1048576.0, 2) if previous_total is not None else 0.0,
            'top': _top_entries(snapshot.statistics('lineno')[: self._top_limit]),
            'growth': _top_entries(growth),
        }
        return row

    def dump_path(self) -> Path:
        return self._output_path.with_name(self._output_path.stem + '-dump.txt')

    def _dump_deep_traces(self, snapshot, traced_current: int) -> None:
        """按调用链（不是单行站点）落一份驻留榜：站点榜只说"在哪申请"，这条链才说"谁在申请"。

        用触发那一拍的快照，不再重新 take_snapshot——峰是瞬时的，第二次采样往往已经掉下去
        （实盘就漏过一次：行里记到 374MB，闸门复查时只剩几十 MB）。
        每进程一次；要有内容，探针必须以 `G3KU_MEM_PROBE_FRAMES`>1 启动，否则链上只有一帧。
        """
        self._dumped = True
        lines = [
            f'# mem-probe deep dump traced_mb={traced_current / 1048576.0:.2f} '
            f'frames={self._frames} limit={self._dump_limit}',
        ]
        for stat in snapshot.statistics('traceback')[: self._dump_limit]:
            chain = ' <- '.join(reversed(stat.traceback.format()))
            lines.append(f'{stat.size / 1048576.0:.2f}MB {stat.count}blk {chain}')
        path = self.dump_path()
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        logger.info('mem probe deep dump written: {}', path)

    def _thread_main(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                row = self.sample_once()
                if row is None:
                    continue
                with self._output_path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                traced_now = float(row['traced_mb'])
                if (
                    not self._dumped
                    and self._dump_threshold_bytes > 0
                    and traced_now * 1048576 >= self._dump_threshold_bytes
                    and self._last_snapshot is not None
                ):
                    self._dump_deep_traces(self._last_snapshot, int(traced_now * 1048576))
            except Exception:
                # 静默死掉的探针会让人把"没有行"当成"没有驻留"，那比没有探针更糟。
                logger.exception('mem probe stopped sampling; rows end here: {}', self._output_path)
                return


def start_mem_probe(*, runtime_dir: Path) -> MemProbe | None:
    """标记文件在则启动探针，输出到 `<runtime_dir>/mem-probe/mem-probe-<pid>.jsonl`。"""

    marker = mem_probe_marker(runtime_dir)
    if not mem_probe_requested(marker):
        return None
    values = probe_settings(marker)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    output_path = Path(runtime_dir) / OUTPUT_DIR_NAME / f'mem-probe-{os.getpid()}-{stamp}.jsonl'
    probe = MemProbe(
        output_path=output_path,
        interval_seconds=values['interval_seconds'],
        top_limit=int(values['top_limit']),
        frames=int(values['frames']),
        dump_threshold_bytes=int(values['dump_mb'] * 1048576),
    )
    probe.start()
    logger.info(
        'mem probe started: interval={}s frames={} top={} dump_mb={} out={}',
        values['interval_seconds'],
        int(values['frames']),
        int(values['top_limit']),
        values['dump_mb'],
        output_path,
    )
    return probe
