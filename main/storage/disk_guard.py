"""磁盘写保护与水位预算（P0 磁盘治理止血包）。

背景：2026-09-09 磁盘满事故中，main/ 侧写入无 ENOSPC 防护——sqlite 的
``OperationalError: database or disk is full`` 从 ``_run_write`` 原样上抛，
节点错误记录路径二次写盘再炸，导致 31 个节点连锁 error-pause。

本模块提供三件事，供 sqlite_store / artifact_store / log_service / runtime 层复用：

1. 异常分类：``is_disk_full_error`` / ``classify_write_error`` / ``DiskFullError``。
   ``DiskFullError`` 继承 ``OSError``，既有 ``except OSError`` 调用方语义不变。
2. 应急写预算：``has_emergency_disk_budget``——"可降级写"（actual_request artifact、
   live patch 外置、execution trace 外置）在磁盘剩余低于 ``max(emergency_min_bytes,
   total * emergency_min_ratio)`` 时直接跳过落盘；关键写（任务/节点状态、pause、
   error_log）不预检、照常尝试，靠分类异常 + 调用点兜底。
3. 策略集中：``DiskPolicies`` 进程级单例，由 runtime_service 启动时从
   ``config.main_runtime.disk_guard`` 注入（``configure_disk_policies``）；
   未注入时回退 ``G3KU_*`` 环境变量（主要供测试/应急覆盖）。
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    'DiskFullError',
    'DiskPolicies',
    'classify_write_error',
    'cleanup_threshold_bytes',
    'configure_disk_policies',
    'disk_policies',
    'disk_waterline_snapshot',
    'emergency_free_bytes_min',
    'emergency_threshold_bytes',
    'has_emergency_disk_budget',
    'invalidate_disk_usage_cache',
    'is_disk_full_error',
]

_DISK_FULL_MESSAGE_MARKERS = (
    'database or disk is full',
    'no space left on device',
    'disk i/o error',
)


class DiskFullError(OSError):
    """磁盘已满的规范化信号；``original`` 保留底层异常供诊断。"""

    def __init__(self, message: str, *, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.errno = errno.ENOSPC
        self.original = original


def is_disk_full_error(exc: BaseException | None) -> bool:
    """识别 sqlite SQLITE_FULL 与文件系统 ENOSPC。"""
    if exc is None:
        return False
    if isinstance(exc, DiskFullError):
        return True
    error_name = str(getattr(exc, 'sqlite_errorname', '') or '').strip().upper()
    if error_name == 'SQLITE_FULL':
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _DISK_FULL_MESSAGE_MARKERS)


def classify_write_error(exc: BaseException) -> BaseException:
    """磁盘满异常 → DiskFullError；其余原样返回。

    ``write_guard_enabled=False`` 时不做包装（回滚开关：恢复事故前的原样上抛行为）。
    """
    if exc is None:
        return exc
    if isinstance(exc, DiskFullError):
        return exc
    if not disk_policies().write_guard_enabled:
        return exc
    if is_disk_full_error(exc):
        return DiskFullError(str(exc), original=exc)
    return exc


@dataclass(frozen=True)
class DiskPolicies:
    write_guard_enabled: bool = True
    emergency_min_bytes: int = 300 * 1024 * 1024
    emergency_min_ratio: float = 0.01
    usage_ttl_seconds: float = 5.0
    artifact_gzip_threshold_bytes: int = 1024 * 1024
    terminal_cleanup_enabled: bool = True
    # P1：清理线（历史任务压缩渐进 + 强收紧触发水位）。
    cleanup_min_bytes: int = 1024 * 1024 * 1024
    cleanup_min_ratio: float = 0.05
    # P1：紧急态行为开关与防抖样本数（1s 采样 tick 计）。
    auto_pause_enabled: bool = True
    emergency_streak_samples: int = 3
    emergency_recovery_samples: int = 5
    alert_on_disk_emergency: bool = True
    # P2：任务压缩归档。
    archive_enabled: bool = True
    archive_sweep_batch: int = 4
    archive_sweep_interval_seconds: float = 15.0
    # 解压宽限：解压后该窗口内不再被压缩渐进重新归档（用户查看/排查保护期）；
    # 0 = 关闭宽限（解压后立即可被重新压缩）。
    decompress_grace_minutes: float = 60.0
    # P3：终态任务大行裁剪与删除渐进。
    detail_retention_days: int = 14  # 0 = 关闭裁剪
    purge_enabled: bool = True


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {'0', 'false', 'no', 'off'}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _policies_from_env() -> DiskPolicies:
    return DiskPolicies(
        write_guard_enabled=_env_flag('G3KU_WRITE_GUARD_ENABLED', True),
        emergency_min_bytes=_env_int('G3KU_DISK_EMERGENCY_MIN_BYTES', 300 * 1024 * 1024),
        emergency_min_ratio=_env_float('G3KU_DISK_EMERGENCY_MIN_RATIO', 0.01),
        usage_ttl_seconds=_env_float('G3KU_DISK_USAGE_TTL_SECONDS', 5.0),
        artifact_gzip_threshold_bytes=_env_int('G3KU_ARTIFACT_GZIP_THRESHOLD_BYTES', 1024 * 1024),
        terminal_cleanup_enabled=_env_flag('G3KU_TERMINAL_CLEANUP_ENABLED', True),
        cleanup_min_bytes=_env_int('G3KU_DISK_CLEANUP_MIN_BYTES', 1024 * 1024 * 1024),
        cleanup_min_ratio=_env_float('G3KU_DISK_CLEANUP_MIN_RATIO', 0.05),
        auto_pause_enabled=_env_flag('G3KU_DISK_AUTO_PAUSE_ENABLED', True),
        emergency_streak_samples=_env_int('G3KU_DISK_EMERGENCY_STREAK_SAMPLES', 3),
        emergency_recovery_samples=_env_int('G3KU_DISK_EMERGENCY_RECOVERY_SAMPLES', 5),
        alert_on_disk_emergency=_env_flag('G3KU_DISK_ALERT_ON_EMERGENCY', True),
        archive_enabled=_env_flag('G3KU_DISK_ARCHIVE_ENABLED', True),
        archive_sweep_batch=_env_int('G3KU_DISK_ARCHIVE_SWEEP_BATCH', 4),
        archive_sweep_interval_seconds=_env_float('G3KU_DISK_ARCHIVE_SWEEP_INTERVAL_SECONDS', 15.0),
        decompress_grace_minutes=_env_float('G3KU_DISK_DECOMPRESS_GRACE_MINUTES', 60.0),
        detail_retention_days=_env_int('G3KU_DISK_DETAIL_RETENTION_DAYS', 14),
        purge_enabled=_env_flag('G3KU_DISK_PURGE_ENABLED', True),
    )


_policies_lock = threading.Lock()
_configured_policies: DiskPolicies | None = None


def disk_policies() -> DiskPolicies:
    """进程级策略单例；未显式 configure 时按环境变量惰性构造。"""
    global _configured_policies
    with _policies_lock:
        if _configured_policies is None:
            _configured_policies = _policies_from_env()
        return _configured_policies


def configure_disk_policies(policies: DiskPolicies | None) -> DiskPolicies:
    """runtime_service 启动时注入配置；传 None 恢复环境变量默认（测试用）。"""
    global _configured_policies
    with _policies_lock:
        _configured_policies = policies if policies is not None else _policies_from_env()
        return _configured_policies


_usage_lock = threading.Lock()
_usage_cache: dict[str, tuple[float, int, int]] = {}


def invalidate_disk_usage_cache() -> None:
    with _usage_lock:
        _usage_cache.clear()


def _resolve_anchor(path: Path) -> Path | None:
    """向上找到存在的盘根锚点（Windows: C:\\；POSIX: /）。找不到返回 None。"""
    current = path
    try:
        current = Path(current).expanduser()
    except Exception:
        return None
    for candidate in (current, *current.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _disk_usage_for_anchor(anchor: Path, ttl_seconds: float) -> tuple[int, int] | None:
    key = str(anchor)
    now = time.monotonic()
    with _usage_lock:
        cached = _usage_cache.get(key)
        if cached is not None and (now - cached[0]) < max(0.0, ttl_seconds):
            return cached[1], cached[2]
    try:
        usage = shutil.disk_usage(str(anchor))
    except OSError:
        return None
    snapshot = (now, int(usage.total), int(usage.free))
    with _usage_lock:
        _usage_cache[key] = snapshot
    return snapshot[1], snapshot[2]


def emergency_free_bytes_min(paths: Iterable[Path | str], *, policies: DiskPolicies | None = None) -> int | None:
    """多路径取各盘剩余字节的最小值；全部探测失败返回 None（调用方保守放行）。"""
    snapshot = disk_waterline_snapshot(paths, policies=policies)
    if snapshot is None:
        return None
    return snapshot[0]


def disk_waterline_snapshot(
    paths: Iterable[Path | str],
    *,
    policies: DiskPolicies | None = None,
) -> tuple[int, int] | None:
    """返回 (free, total)：取剩余空间最小的盘的 free 与该盘的 total；全部探测失败返回 None。

    带 TTL 缓存（policies.usage_ttl_seconds），供 1s 级压力监控采样复用，不逐次 statfs。
    """
    resolved = policies or disk_policies()
    anchors: list[Path] = []
    seen: set[str] = set()
    for raw in paths or ():
        anchor = _resolve_anchor(Path(str(raw or '.')))
        if anchor is None:
            continue
        key = str(anchor)
        if key in seen:
            continue
        seen.add(key)
        anchors.append(anchor)
    if not anchors:
        return None
    entries: list[tuple[int, int]] = []
    for anchor in anchors:
        usage = _disk_usage_for_anchor(anchor, resolved.usage_ttl_seconds)
        if usage is None:
            continue
        total, free = usage
        entries.append((free, total))
    if not entries:
        return None
    free, total = min(entries, key=lambda item: item[0])
    return free, total


def emergency_threshold_bytes(total_bytes: int, *, policies: DiskPolicies | None = None) -> int:
    """紧急线 = max(emergency_min_bytes, total * emergency_min_ratio)。"""
    resolved = policies or disk_policies()
    return max(int(resolved.emergency_min_bytes), int(max(0, int(total_bytes)) * float(resolved.emergency_min_ratio)))


def cleanup_threshold_bytes(total_bytes: int, *, policies: DiskPolicies | None = None) -> int:
    """清理线 = max(cleanup_min_bytes, total * cleanup_min_ratio)。"""
    resolved = policies or disk_policies()
    return max(int(resolved.cleanup_min_bytes), int(max(0, int(total_bytes)) * float(resolved.cleanup_min_ratio)))


def has_emergency_disk_budget(paths: Sequence[Path | str], *, policies: DiskPolicies | None = None) -> bool:
    """剩余空间是否高于紧急线 max(emergency_min_bytes, total * emergency_min_ratio)。

    - write_guard 关闭：恒 True（回滚开关）。
    - 探测失败（盘根不存在等）：保守放行，绝不因预检自身故障阻断可降级写之外的路径。
    """
    resolved = policies or disk_policies()
    if not resolved.write_guard_enabled:
        return True
    snapshot = disk_waterline_snapshot(paths, policies=resolved)
    if snapshot is None:
        return True
    free, total = snapshot
    return free >= emergency_threshold_bytes(total, policies=resolved)
