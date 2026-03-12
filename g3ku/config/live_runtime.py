from __future__ import annotations

import threading
from pathlib import Path

from loguru import logger

from g3ku.config.loader import get_config_path, load_config
from g3ku.config.schema import Config


class RuntimeConfigManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path = get_config_path()
        self._mtime_ns = -1
        self._revision = 0
        self._last_good: Config | None = None

    def get_runtime_config(self, force: bool = False) -> tuple[Config, int, bool]:
        with self._lock:
            path = Path(self._path)
            try:
                mtime_ns = path.stat().st_mtime_ns
            except FileNotFoundError:
                if self._last_good is not None:
                    return self._last_good, self._revision, False
                raise

            should_reload = bool(force) or self._last_good is None or mtime_ns != self._mtime_ns
            if not should_reload and self._last_good is not None:
                return self._last_good, self._revision, False

            try:
                config = load_config(path)
            except Exception as exc:
                if self._last_good is None:
                    raise
                logger.error("Runtime config reload failed; keeping revision {}: {}", self._revision, exc)
                return self._last_good, self._revision, False

            self._last_good = config
            self._mtime_ns = mtime_ns
            self._revision += 1
            return config, self._revision, True

    def peek_runtime_revision(self) -> int:
        with self._lock:
            return int(self._revision or 0)


_MANAGER = RuntimeConfigManager()


def get_runtime_config(force: bool = False) -> tuple[Config, int, bool]:
    return _MANAGER.get_runtime_config(force=force)


def peek_runtime_revision() -> int:
    return _MANAGER.peek_runtime_revision()
