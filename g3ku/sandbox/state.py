"""沙箱侧的进程内状态：会话档位意图 + 安装态读数。

两样东西都必须是**进程内缓存**：

* 档位意图（`mode`）：每个会话一个值，每回合解析档位都要读它。放磁盘上逐跳读会把
  µs 级判定变成毫秒级，所以启动时读一次、之后只读写内存，变更时整份原子落盘。
* 安装态：探测要跑子进程（`bwrap --version` / `wsl -l` / `docker info`），
  结论在进程内缓存；超过 ``INSTALL_STATE_TTL_SECONDS`` 后由下一次读取**在后台
  重探**（stale-while-revalidate，单飞），读取本身永远返回缓存值，不把子进程
  探测塞进回合构造。`refresh_install_state()` 仍是同步强制重探。

落点：`<数据根>/sandbox/session-modes.json`。数据根而不是工作区——档位是运行时
状态，不是项目配置；换工作区不该丢会话的开关。
"""

from __future__ import annotations

import json
import threading
import time
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
_ROOTS: dict[str, list[str]] = {}
_TOOLS: dict[str, list[str]] = {}
_INSTALL: dict[str, Any] | None = None
_INSTALL_CHECKED_AT: float = 0.0
_INSTALL_PROBING = False
# 安装态的后台重探间隔。探测要起子进程（wsl.exe / bwrap），不能每回合都跑；
# 但也不能像以前那样缓存到进程结束——机器侧修好了/坏了，下一回合就该看到。
INSTALL_STATE_TTL_SECONDS = 60.0
_MAX_WRITABLE_ROOTS = 16
_MAX_ALLOWED_TOOLS = 256


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
    roots = payload.get("writable_roots") if isinstance(payload, dict) else None
    if isinstance(roots, dict):
        _ROOTS.clear()
        for key, value in roots.items():
            normalized_key = str(key or "").strip()
            if normalized_key and isinstance(value, list):
                cleaned = _normalized_roots(value)
                if cleaned:
                    _ROOTS[normalized_key] = cleaned
    tools = payload.get("allowed_tools") if isinstance(payload, dict) else None
    if isinstance(tools, dict):
        _TOOLS.clear()
        for key, value in tools.items():
            normalized_key = str(key or "").strip()
            if normalized_key and isinstance(value, list):
                cleaned = _normalized_tool_names(value)
                if cleaned:
                    _TOOLS[normalized_key] = cleaned
    _MODES = modes
    return modes


def _normalized_roots(values: Any) -> list[str]:
    """额外可写根：只收绝对路径，去重、限数量。

    POSIX 绝对路径（``/home/me/out``）在 Windows 上也收：owner 的 jail 跑在 WSL 发行版里，
    操作员填的就是发行版内路径。相对路径没有锚点，直接丢。
    """
    from pathlib import Path

    roots: list[str] = []
    for item in list(values or []):
        text = str(item or "").strip()
        if not text:
            continue
        posix_absolute = text.startswith("/")
        try:
            resolved = Path(text).expanduser()
        except (OSError, RuntimeError, ValueError):
            continue
        host_absolute = resolved.is_absolute()
        if not host_absolute and not posix_absolute:
            continue
        normalized = str(resolved) if host_absolute else text.rstrip("/")
        if normalized and normalized not in roots:
            roots.append(normalized)
        if len(roots) >= _MAX_WRITABLE_ROOTS:
            break
    return roots


def _normalized_tool_names(values: Any) -> list[str]:
    """工具名：去空、去重、保留首次出现顺序，限数量。"""
    tools: list[str] = []
    seen: set[str] = set()
    for item in list(values or []):
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        tools.append(text)
        if len(tools) >= _MAX_ALLOWED_TOOLS:
            break
    return tools


def get_writable_roots(session_key: Any) -> list[str]:
    """owner 档的额外可写根（D14：交付物常落在桌面/输出目录）。third_party 恒为空。"""
    key = str(session_key or "").strip()
    if not key:
        return []
    with _LOCK:
        _load_locked()
        return list(_ROOTS.get(key) or [])


def set_writable_roots(session_key: Any, values: Any) -> list[str]:
    key = str(session_key or "").strip()
    if not key:
        raise ValueError("session_key_required")
    cleaned = _normalized_roots(values)
    with _LOCK:
        _load_locked()
        if cleaned:
            _ROOTS[key] = cleaned
        else:
            _ROOTS.pop(key, None)
        _write_locked(dict(_MODES or {}))
    return cleaned


def get_session_allowed_tools(session_key: Any) -> list[str]:
    """受限模式的按会话工具 override；空列表 = 未自定义（使用默认集合）。"""
    key = str(session_key or "").strip()
    if not key:
        return []
    with _LOCK:
        _load_locked()
        return list(_TOOLS.get(key) or [])


def set_session_allowed_tools(session_key: Any, values: Any) -> list[str]:
    key = str(session_key or "").strip()
    if not key:
        raise ValueError("session_key_required")
    cleaned = _normalized_tool_names(values)
    with _LOCK:
        _load_locked()
        if cleaned:
            _TOOLS[key] = cleaned
        else:
            _TOOLS.pop(key, None)
        _write_locked(dict(_MODES or {}))
    return cleaned


def clear_session_allowed_tools(session_key: Any) -> None:
    key = str(session_key or "").strip()
    if not key:
        return
    with _LOCK:
        _load_locked()
        if _TOOLS.pop(key, None) is None:
            return
        _write_locked(dict(_MODES or {}))


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
        removed = modes.pop(key, None) is not None
        removed = _TOOLS.pop(key, None) is not None or removed
        removed = _ROOTS.pop(key, None) is not None or removed
        removed = _UNLOCKS.pop(key, None) is not None or removed
        if not removed:
            return
        _write_locked(modes)
        _MODES.clear()
        _MODES.update(modes)


def _write_locked(modes: dict[str, str]) -> None:
    global _MODES_PATH
    path = _MODES_PATH or _states_path()
    _MODES_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "sessions": dict(sorted(modes.items())),
        "operator_unlocks": _UNLOCKS,
        "writable_roots": {key: list(value) for key, value in sorted(_ROOTS.items())},
        "allowed_tools": {key: list(value) for key, value in sorted(_TOOLS.items())},
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def note_operator_unlock(session_key: Any, *, at: str, by: str = "operator") -> None:
    """记一条"操作员用口令确认过一次改档（开或关）"的审计痕迹。"""
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


def _schedule_install_probe() -> None:
    """后台重探安装态；同一时刻最多一个探测线程。"""
    global _INSTALL, _INSTALL_CHECKED_AT, _INSTALL_PROBING
    with _LOCK:
        if _INSTALL_PROBING:
            return
        _INSTALL_PROBING = True

    def run() -> None:
        global _INSTALL, _INSTALL_CHECKED_AT, _INSTALL_PROBING
        reading: dict[str, Any] | None = None
        try:
            reading = probe_install_state()
        except Exception as exc:  # noqa: BLE001 - 探测失败沿用旧读数
            logger.warning("sandbox install re-probe failed: {}", exc)
        with _LOCK:
            if reading is not None:
                _INSTALL = reading
            # 失败也刷新时间戳：否则每个回合都会再起一个必败的探测线程。
            _INSTALL_CHECKED_AT = time.monotonic()
            _INSTALL_PROBING = False

    threading.Thread(target=run, name="sandbox-install-probe", daemon=True).start()


def install_state(*, refresh: bool = False) -> dict[str, Any]:
    """当前机器的安装态读数（进程内缓存，过期由后台重探）。

    首次读取同步探测（调用方需要真实结论）；``refresh=True`` 同步重探；
    其余读取在超过 TTL 时返回旧值并触发一次后台重探。
    """
    global _INSTALL, _INSTALL_CHECKED_AT
    with _LOCK:
        if _INSTALL is None or refresh:
            _INSTALL = probe_install_state()
            _INSTALL_CHECKED_AT = time.monotonic()
            return dict(_INSTALL)
        stale = (time.monotonic() - _INSTALL_CHECKED_AT) > INSTALL_STATE_TTL_SECONDS
        reading = dict(_INSTALL)
    if stale:
        _schedule_install_probe()
    return reading


def install_state_name() -> str:
    return str(install_state().get("state") or INSTALL_NOT_INSTALLED)


def refresh_install_state() -> dict[str, Any]:
    return install_state(refresh=True)


def reset_sandbox_state_for_tests() -> None:
    """测试钩子：丢掉进程内缓存（不动磁盘）。"""
    global _MODES, _MODES_PATH, _INSTALL, _INSTALL_CHECKED_AT, _INSTALL_PROBING
    with _LOCK:
        _MODES = None
        _MODES_PATH = None
        _INSTALL = None
        _INSTALL_CHECKED_AT = 0.0
        _INSTALL_PROBING = False
        _UNLOCKS.clear()
        _ROOTS.clear()
        _TOOLS.clear()


__all__ = [
    "clear_session_allowed_tools",
    "clear_session_mode",
    "get_session_allowed_tools",
    "get_session_mode",
    "get_writable_roots",
    "install_state",
    "install_state_name",
    "note_operator_unlock",
    "operator_unlock_note",
    "refresh_install_state",
    "reset_sandbox_state_for_tests",
    "set_session_allowed_tools",
    "set_session_mode",
    "set_writable_roots",
]
