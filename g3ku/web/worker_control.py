from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from loguru import logger

from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV, get_bootstrap_security_service


WEB_AUTO_WORKER_ENV = "G3KU_WEB_AUTO_WORKER"
WEB_KEEP_WORKER_ENV = "G3KU_WEB_KEEP_WORKER"

_WORKER_LOCK = threading.RLock()
_MANAGED_WORKER_PROCESS: subprocess.Popen | None = None


def _env_enabled(name: str) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def auto_worker_enabled() -> bool:
    return _env_enabled(WEB_AUTO_WORKER_ENV)


def keep_worker_enabled() -> bool:
    return _env_enabled(WEB_KEEP_WORKER_ENV)


def _active_process_locked() -> subprocess.Popen | None:
    global _MANAGED_WORKER_PROCESS

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
    return None


def managed_worker_pid() -> int | None:
    with _WORKER_LOCK:
        process = _active_process_locked()
        if process is None:
            return None
        return int(getattr(process, "pid", 0) or 0) or None


def start_managed_task_worker() -> bool:
    global _MANAGED_WORKER_PROCESS

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
            },
        }
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        if creationflags:
            popen_kwargs["creationflags"] = creationflags

        process = subprocess.Popen([sys.executable, "-m", "g3ku", "worker"], **popen_kwargs)
        _MANAGED_WORKER_PROCESS = process

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
    global _MANAGED_WORKER_PROCESS

    if keep_worker_enabled():
        return

    with _WORKER_LOCK:
        process = _MANAGED_WORKER_PROCESS
        _MANAGED_WORKER_PROCESS = None

    if process is None:
        return

    pid = getattr(process, "pid", None)
    await asyncio.to_thread(_stop_process, process)
    logger.info("Stopped managed task worker pid={}", pid)


__all__ = [
    "WEB_AUTO_WORKER_ENV",
    "WEB_KEEP_WORKER_ENV",
    "auto_worker_enabled",
    "ensure_managed_task_worker",
    "keep_worker_enabled",
    "managed_worker_pid",
    "shutdown_managed_task_worker",
    "start_managed_task_worker",
    "wait_for_task_worker_online",
]
