"""Concrete runtime engine for g3ku agent execution."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from importlib.machinery import SourcelessFileLoader
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.legacy import direct_runtime as legacy_direct_runtime

_THIS_FILE = Path(__file__).resolve()
_BASE_DIR = _THIS_FILE.parent if _THIS_FILE.parent.name != "__pycache__" else _THIS_FILE.parent.parent
_PYC_PATH = _BASE_DIR / "__pycache__" / f"engine.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
if not _PYC_PATH.exists():
    raise RuntimeError(f"Missing runtime engine bytecode at {_PYC_PATH}")

_loader = SourcelessFileLoader("g3ku.runtime._engine_pyc", str(_PYC_PATH))
_spec = importlib.util.spec_from_loader("g3ku.runtime._engine_pyc", _loader)
if _spec is None:
    raise RuntimeError(f"Unable to build module spec for {_PYC_PATH}")
_pyc_module = importlib.util.module_from_spec(_spec)
_loader.exec_module(_pyc_module)

for _name, _value in vars(_pyc_module).items():
    if not _name.startswith("__"):
        globals().setdefault(_name, _value)

AgentRuntimeEngine = _pyc_module.AgentRuntimeEngine

_original_init = AgentRuntimeEngine.__init__
_original_close_mcp = AgentRuntimeEngine.close_mcp


def _patched_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    if not hasattr(self, "_checkpointer_lock"):
        self._checkpointer_lock = asyncio.Lock()
    if not hasattr(self, "_session_notices"):
        self._session_notices = {}


def _checkpointer_health(self) -> tuple[bool, str]:
    checkpointer = getattr(self, "_checkpointer", None)
    if checkpointer is None:
        return False, "missing"
    conn = getattr(checkpointer, "conn", None)
    if conn is None:
        return True, "ready"
    running = getattr(conn, "_running", True)
    connection = getattr(conn, "_connection", object())
    if running is False:
        return False, "connection_stopped"
    if connection is None:
        return False, "connection_closed"
    return True, "ready"


async def _dispose_checkpointer(self) -> None:
    checkpointer = getattr(self, "_checkpointer", None)
    if checkpointer is not None and hasattr(checkpointer, "close"):
        try:
            maybe = checkpointer.close()
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            logger.debug("Checkpointer close skipped")
    checkpointer_cm = getattr(self, "_checkpointer_cm", None)
    if checkpointer_cm is not None:
        try:
            if hasattr(checkpointer_cm, "__aexit__"):
                maybe = checkpointer_cm.__aexit__(None, None, None)
                if inspect.isawaitable(maybe):
                    await maybe
            else:
                checkpointer_cm.__exit__(None, None, None)
        except Exception:
            logger.debug("Checkpointer context close skipped")
    self._checkpointer = None
    self._checkpointer_cm = None


@staticmethod
def _is_recoverable_checkpointer_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(token in text for token in [
        "connection closed",
        "closed database",
        "cannot operate on a closed database",
        "no active connection",
        "checkpointer",
        "sqlite",
        "aiosqlite",
    ])


def record_session_notice(self, session_key: str | None, *, source: str, level: str = "warn", text: str, metadata: dict[str, Any] | None = None) -> None:
    key = str(session_key or "").strip()
    if not key:
        return
    notice_text = str(text or "").strip()
    if not notice_text:
        return
    self._session_notices.setdefault(key, []).append(
        {
            "source": str(source or "runtime"),
            "level": str(level or "warn"),
            "text": notice_text,
            "metadata": dict(metadata or {}),
        }
    )


def drain_session_notices(self, session_key: str | None) -> list[dict[str, Any]]:
    key = str(session_key or "").strip()
    if not key:
        return []
    return self._session_notices.pop(key, [])


async def _patched_ensure_checkpointer_ready(self) -> None:
    if not getattr(self, "_checkpointer_enabled", False):
        return
    if getattr(self, "_checkpointer_backend", "") != "sqlite" or getattr(self, "_checkpointer_path", None) is None:
        return

    async with self._checkpointer_lock:
        healthy, reason = _checkpointer_health(self)
        if healthy:
            return
        if getattr(self, "_checkpointer", None) is not None or getattr(self, "_checkpointer_cm", None) is not None:
            logger.warning("Async SQLite checkpointer became unusable ({}); recreating it.", reason)
            await _dispose_checkpointer(self)

        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(str(self._checkpointer_path))
            self._checkpointer = await self._checkpointer_cm.__aenter__()
        except Exception as exc:
            await _dispose_checkpointer(self)
            if _is_recoverable_checkpointer_error(exc):
                logger.warning("Async SQLite checkpointer reopen failed; continuing without it for this turn: {}", exc)
                return
            logger.warning(
                "Async SQLite checkpointer init failed, fallback to session-file history: {}",
                exc,
            )
            self._checkpointer_enabled = False
            self._checkpointer_backend = "disabled"
            self._checkpointer_path = None


async def _patched_close_mcp(self) -> None:
    await _original_close_mcp(self)
    self._checkpointer = None
    self._checkpointer_cm = None


AgentRuntimeEngine.__init__ = _patched_init
AgentRuntimeEngine._checkpointer_health = _checkpointer_health
AgentRuntimeEngine._dispose_checkpointer = _dispose_checkpointer
AgentRuntimeEngine._is_recoverable_checkpointer_error = _is_recoverable_checkpointer_error
AgentRuntimeEngine.record_session_notice = record_session_notice
AgentRuntimeEngine.drain_session_notices = drain_session_notices
AgentRuntimeEngine._ensure_checkpointer_ready = _patched_ensure_checkpointer_ready
AgentRuntimeEngine.close_mcp = _patched_close_mcp

__all__ = getattr(_pyc_module, "__all__", ["AgentRuntimeEngine", "legacy_direct_runtime"])
if "AgentRuntimeEngine" not in __all__:
    __all__ = [*__all__, "AgentRuntimeEngine"]
if "legacy_direct_runtime" not in __all__:
    __all__ = [*__all__, "legacy_direct_runtime"]

