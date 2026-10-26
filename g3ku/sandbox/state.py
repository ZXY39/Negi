"""沙箱侧的进程内状态：会话档位意图 + 安装态读数。

两样东西都必须是**进程内缓存**：

* 档位意图（`mode`）：每个会话一个值，每回合解析档位都要读它。放磁盘上逐跳读会把
  µs 级判定变成毫秒级，所以启动时读一次、之后只读写内存，变更时整份原子落盘。
* 安装态：探测要跑子进程（`bwrap --version` / `wsl -l` / `docker info`），
  结论缓存到进程生命周期；只有显式 `refresh_install_state()` 才重探。

落点：`<数据根>/sandbox/session-modes.json`。数据根而不是工作区——档位是运行时
状态，不是项目配置；换工作区不该丢会话的开关。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.sandbox.policy import (
    INSTALL_NOT_INSTALLED,
    MODE_RESTRICTED,
    MODE_UNRESTRICTED,
)
from g3ku.sandbox.probe import probe_install_state

_LOCK = threading.RLock()
_MODES: dict[str, str] | None = None
_MODES_PATH: Path | None = None
_UNLOCKS: dict[str, dict[str, str]] = {}
_INSTALL: dict[str, Any] | None = None


def _normalized_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in {MODE_RESTRICTED, MODE_UNRESTRICTED} else ""


def _states_path() -> Path:
    from g3ku.deployment.data_root import data_work_path

    return data_work_path("sandbox") / "session-modes.json"


def _load_locked() -> dict[str, str]:
    global _MODES, _MODES_PATH
    if _MODES is not None:
        return _MODES
    path = _states_path()
    _MODES_PATH = path
    modes: dict[str, str] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:  # noqa: BLE001 - 坏文件按空处理，不阻塞启动
        logger.warning("sandbox session modes unreadable, starting empty: {}", exc)
        payload = {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if isinstance(sessions, dict):
        for key, value in sessions.items():
            normalized_key = str(key or "").strip()
            mode = _normalized_mode(value)
            if normalized_key and mode:
                modes[normalized_key] = mode
    unlocks = payload.get("operator_unlocks") if isinstance(payload, dict) else None
    if isinstance(unlocks, dict):
        _UNLOCKS.clear()
        for key, value in unlocks.items():
            normalized_key = str(key or "").strip()
            if normalized_key and isinstance(value, dict):
                _UNLOCKS[normalized_key] = {
                    "at": str(value.get("at") or ""),
                    "by": str(value.get("by") or ""),
                }
    _MODES = modes
    return modes


def get_session_mode(session_key: Any) -> str:
    """会话的档位意图；空串 = 未设置（按信任派生的默认）。"""
    key = str(session_key or "").strip()
    if not key:
        return ""
    with _LOCK:
        return _load_locked().get(key, "")


def set_session_mode(session_key: Any, mode: Any) -> str:
    key = str(session_key or "").strip()
    normalized = _normalized_mode(mode)
    if not key or not normalized:
        raise ValueError("invalid_session_mode")
    with _LOCK:
        modes = dict(_load_locked())
        if modes.get(key) == normalized:
            return normalized
        modes[key] = normalized
        _write_locked(modes)
        _MODES.clear()
        _MODES.update(modes)
    return normalized


def clear_session_mode(session_key: Any) -> None:
    key = str(session_key or "").strip()
    if not key:
        return
    with _LOCK:
        modes = dict(_load_locked())
        if modes.pop(key, None) is None:
            return
        _write_locked(modes)
        _MODES.clear()
        _MODES.update(modes)


def _write_locked(modes: dict[str, str]) -> None:
    global _MODES_PATH
    path = _MODES_PATH or _states_path()
    _MODES_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "sessions": dict(sorted(modes.items())), "operator_unlocks": _UNLOCKS}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def note_operator_unlock(session_key: Any, *, at: str, by: str = "operator") -> None:
    """记一条"操作员用口令把该会话放到 unrestricted"的审计痕迹。"""
    key = str(session_key or "").strip()
    if not key:
        return
    with _LOCK:
        _load_locked()
        _UNLOCKS[key] = {"at": str(at or ""), "by": str(by or "")}
        _write_locked(dict(_MODES or {}))


def operator_unlock_note(session_key: Any) -> dict[str, Any]:
    key = str(session_key or "").strip()
    if not key:
        return {}
    with _LOCK:
        _load_locked()
        return dict(_UNLOCKS.get(key) or {})


def install_state(*, refresh: bool = False) -> dict[str, Any]:
    """当前机器的安装态读数（进程内缓存）。"""
    global _INSTALL
    with _LOCK:
        if _INSTALL is None or refresh:
            _INSTALL = probe_install_state()
        return dict(_INSTALL)


def install_state_name() -> str:
    return str(install_state().get("state") or INSTALL_NOT_INSTALLED)


def refresh_install_state() -> dict[str, Any]:
    return install_state(refresh=True)


def reset_sandbox_state_for_tests() -> None:
    """测试钩子：丢掉进程内缓存（不动磁盘）。"""
    global _MODES, _MODES_PATH, _INSTALL
    with _LOCK:
        _MODES = None
        _MODES_PATH = None
        _INSTALL = None
        _UNLOCKS.clear()


__all__ = [
    "clear_session_mode",
    "get_session_mode",
    "install_state",
    "install_state_name",
    "note_operator_unlock",
    "operator_unlock_note",
    "refresh_install_state",
    "reset_sandbox_state_for_tests",
    "set_session_mode",
]
