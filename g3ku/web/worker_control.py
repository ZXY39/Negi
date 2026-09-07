from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from loguru import logger

from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV, get_bootstrap_security_service
from g3ku.web.windows_job import assign_process_to_kill_on_close_job


WEB_AUTO_WORKER_ENV = "G3KU_WEB_AUTO_WORKER"
WEB_KEEP_WORKER_ENV = "G3KU_WEB_KEEP_WORKER"

_WORKER_LOCK = threading.RLock()
_MANAGED_WORKER_PROCESS: subprocess.Popen | None = None
_MANAGED_WORKER_STARTED_AT_MONOTONIC: float | None = None
_MANAGED_WORKER_STARTED_AT: str = ""
_MANAGED_WORKER_STARTING_GRACE_SECONDS = 10.0
_MANAGED_WORKER_LOG_RELATIVE_PATH = Path(".g3ku") / "main-runtime" / "managed-worker.log"


def _env_enabled(name: str) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def auto_worker_enabled() -> bool:
    return _env_enabled(WEB_AUTO_WORKER_ENV)


def keep_worker_enabled() -> bool:
    return _env_enabled(WEB_KEEP_WORKER_ENV)


def _active_process_locked() -> subprocess.Popen | None:
    global _MANAGED_WORKER_PROCESS, _MANAGED_WORKER_STARTED_AT_MONOTONIC, _MANAGED_WORKER_STARTED_AT

    process = _MANAGED_WORKER_PROCESS
    if process is None:
        return None
    if process.poll() is None:
        return process

    logger.warning(
        "Managed task worker exited pid={} code={}",
        getattr(process, "pid", None),
        getattr(process, "returncode", None),
    )
    _MANAGED_WORKER_PROCESS = None
    _MANAGED_WORKER_STARTED_AT_MONOTONIC = None
    _MANAGED_WORKER_STARTED_AT = ""
    return None


def managed_worker_pid() -> int | None:
    with _WORKER_LOCK:
        process = _active_process_locked()
        if process is None:
            return None
        return int(getattr(process, "pid", 0) or 0) or None


def managed_worker_snapshot(*, starting_grace_s: float = _MANAGED_WORKER_STARTING_GRACE_SECONDS) -> dict[str, object]:
    with _WORKER_LOCK:
        process = _active_process_locked()
        pid = int(getattr(process, "pid", 0) or 0) or None if process is not None else None
        started_at = str(_MANAGED_WORKER_STARTED_AT or "").strip()
        started_at_monotonic = _MANAGED_WORKER_STARTED_AT_MONOTONIC
    grace_seconds = max(0.0, float(starting_grace_s or 0.0))
    starting = bool(
        process is not None
        and started_at_monotonic is not None
        and grace_seconds > 0
        and (time.monotonic() - started_at_monotonic) <= grace_seconds
    )
    return {
        "pid": pid,
        "active": process is not None,
        "auto_worker_enabled": auto_worker_enabled(),
        "started_at": started_at,
        "starting": starting,
        "starting_grace_seconds": grace_seconds,
    }


def _managed_worker_log_path() -> Path:
    return Path.cwd() / _MANAGED_WORKER_LOG_RELATIVE_PATH


def start_managed_task_worker() -> bool:
    global _MANAGED_WORKER_PROCESS, _MANAGED_WORKER_STARTED_AT_MONOTONIC, _MANAGED_WORKER_STARTED_AT

    if not auto_worker_enabled():
        return False

    with _WORKER_LOCK:
        process = _active_process_locked()
        if process is not None:
            return False

        security = get_bootstrap_security_service(Path.cwd())
        master_key = security.active_master_key()
        if not master_key:
            logger.warning("Managed task worker start skipped because the current web process is not unlocked")
            return False

        popen_kwargs: dict[str, Any] = {
            "cwd": str(Path.cwd()),
            "env": {
                **os.environ.copy(),
                BOOTSTRAP_MASTER_KEY_ENV: master_key,
                "G3KU_TASK_RUNTIME_ROLE": "worker",
            },
        }
        popen_kwargs["env"].pop(WEB_AUTO_WORKER_ENV, None)
        popen_kwargs["env"].pop(WEB_KEEP_WORKER_ENV, None)
        log_path = _managed_worker_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        popen_kwargs["stdout"] = log_handle
        popen_kwargs["stderr"] = subprocess.STDOUT
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        if creationflags:
            popen_kwargs["creationflags"] = creationflags

        process = subprocess.Popen([sys.executable, "-m", "g3ku", "worker"], **popen_kwargs)
        if os.name == "nt" and not assign_process_to_kill_on_close_job(process):
            logger.debug("Managed task worker job-object binding skipped pid={}", getattr(process, "pid", None))
        _MANAGED_WORKER_PROCESS = process
        _MANAGED_WORKER_STARTED_AT_MONOTONIC = time.monotonic()
        _MANAGED_WORKER_STARTED_AT = datetime.now(timezone.utc).isoformat()

    logger.info("Started managed task worker pid={} cwd={}", process.pid, Path.cwd())
    return True


async def wait_for_task_worker_online(
    service: Any | None,
    *,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.1,
) -> bool:
    if service is None or not hasattr(service, "is_worker_online"):
        return False

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            if bool(service.is_worker_online()):
                return True
        except Exception:
            return False
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(max(0.01, float(poll_interval_s)))

    try:
        return bool(service.is_worker_online())
    except Exception:
        return False


async def ensure_managed_task_worker(
    service: Any | None = None,
    *,
    wait_timeout_s: float = 5.0,
) -> bool:
    if not auto_worker_enabled():
        return False

    started = start_managed_task_worker()
    if service is not None:
        await wait_for_task_worker_online(service, timeout_s=wait_timeout_s)
    return started


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        except Exception:
            logger.warning("Managed task worker kill fallback failed pid={}", getattr(process, "pid", None))


async def shutdown_managed_task_worker() -> None:
    global _MANAGED_WORKER_PROCESS, _MANAGED_WORKER_STARTED_AT_MONOTONIC, _MANAGED_WORKER_STARTED_AT

    if keep_worker_enabled():
        return

    with _WORKER_LOCK:
        process = _MANAGED_WORKER_PROCESS
        _MANAGED_WORKER_PROCESS = None
        _MANAGED_WORKER_STARTED_AT_MONOTONIC = None
        _MANAGED_WORKER_STARTED_AT = ""

    if process is None:
        return

    pid = getattr(process, "pid", None)
    await asyncio.to_thread(_stop_process, process)
    logger.info("Stopped managed task worker pid={}", pid)


_WATCHDOG_POLL_SECONDS = 5.0
_WATCHDOG_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 30.0, 60.0)
_TASK_WORKER_LEASE_ROLE = "task_worker"


def _pid_alive(pid: int | None) -> bool | None:
    """Best-effort process liveness probe.

    Returns True (alive), False (dead) or None (unknown). Callers treat None as
    "do not take destructive action", keeping the watchdog conservative when no
    reliable OS probe is available.
    """
    normalized_pid = int(pid or 0)
    if normalized_pid <= 0:
        return False
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(normalized_pid))
        except Exception:
            return None
    if os.name == "nt":
        return None
    try:
        os.kill(normalized_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return None


def _settle_worker_lease_for_restart(service: Any) -> bool:
    """Decide whether the watchdog may spawn a managed worker now.

    Returns False while a *live* process still holds the task-worker lease
    (for example a separately-launched worker) — spawning then would only
    churn, and could race into double execution. When the holder pid is
    confirmed dead, the stale lease row is removed for that specific worker id
    so the fresh worker does not have to wait out the lease TTL. Unknown
    liveness leaves the lease untouched: the fresh worker's own acquisition
    remains the single arbiter.
    """
    if service is None:
        return True
    store = getattr(service, "store", None)
    get_lease = getattr(store, "get_worker_lease", None)
    if not callable(get_lease):
        return True
    try:
        lease = get_lease(_TASK_WORKER_LEASE_ROLE)
    except Exception:
        return True
    if not lease:
        return True
    worker_id = str(lease.get("worker_id") or "").strip()
    holder_pid = int(lease.get("holder_pid") or 0)
    alive = _pid_alive(holder_pid)
    if alive is True:
        logger.info(
            "managed task worker watchdog: live lease holder pid={} worker_id={}; skip respawn",
            holder_pid,
            worker_id,
        )
        return False
    if alive is False and worker_id:
        release_lease = getattr(store, "release_worker_lease", None)
        if callable(release_lease):
            try:
                release_lease(role=_TASK_WORKER_LEASE_ROLE, worker_id=worker_id)
                logger.info(
                    "managed task worker watchdog: released stale lease of dead holder worker_id={}",
                    worker_id,
                )
            except Exception as exc:
                logger.warning("managed task worker watchdog: stale lease release failed: {}", exc)
    return True


async def run_managed_task_worker_watchdog(service: Any | None = None) -> None:
    """Supervise the managed task worker and restart it when it dies.

    The trigger is the managed *process* being gone, never a merely stale
    heartbeat, so a stalled-but-alive worker is not mistaken for dead. The
    watchdog refuses to fight a live lease holder, and backs off exponentially
    to bound restart churn on persistent startup failures.
    """
    backoff_index = 0
    while True:
        delay = _WATCHDOG_POLL_SECONDS
        try:
            if not auto_worker_enabled():
                return
            if managed_worker_pid() is not None:
                backoff_index = 0
            elif not _settle_worker_lease_for_restart(service):
                backoff_index = 0
            elif start_managed_task_worker():
                if service is not None:
                    await wait_for_task_worker_online(service, timeout_s=2.0)
                backoff_index = 0
            else:
                backoff_index = min(backoff_index + 1, len(_WATCHDOG_BACKOFF_SECONDS) - 1)
                delay = _WATCHDOG_BACKOFF_SECONDS[backoff_index]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("managed task worker watchdog tick failed: {}", exc)
            backoff_index = min(backoff_index + 1, len(_WATCHDOG_BACKOFF_SECONDS) - 1)
            delay = _WATCHDOG_BACKOFF_SECONDS[backoff_index]
        await asyncio.sleep(delay)


__all__ = [
    "WEB_AUTO_WORKER_ENV",
    "WEB_KEEP_WORKER_ENV",
    "auto_worker_enabled",
    "ensure_managed_task_worker",
    "keep_worker_enabled",
    "managed_worker_pid",
    "managed_worker_snapshot",
    "run_managed_task_worker_watchdog",
    "shutdown_managed_task_worker",
    "start_managed_task_worker",
    "wait_for_task_worker_online",
]
