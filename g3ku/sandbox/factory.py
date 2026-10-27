"""按安装态读数构造 ``SandboxManager``。

工厂只在安装态是 ``ready`` / ``partial`` 时返回实例；后端按探测选中的那一个构造成
对应类。**这一步不看闸门**：闸门（``policy.jail_backends_available``）决定"会不会
真的发放 `jailed`"，工厂只回答"如果发放，用哪个后端、卷放哪"。

进程内单例：探测结论本来就缓存，manager 里的会话卷句柄与 lease 也必须全进程唯一，
否则同一会话会被两处独立计租。
"""

from __future__ import annotations

import threading
from typing import Any

from loguru import logger

from g3ku.sandbox.backend_base import Enforcement, SandboxBackend
from g3ku.sandbox.backend_bwrap import BwrapBackend
from g3ku.sandbox.backend_container import ContainerBackend
from g3ku.sandbox.backend_seatbelt import SeatbeltBackend
from g3ku.sandbox.backend_wsl import WslBwrapBackend
from g3ku.sandbox.manager import SandboxManager
from g3ku.sandbox.state import install_state

_MANAGER: SandboxManager | None = None
_MANAGER_LOCK = threading.RLock()
_USABLE_STATES = frozenset({"ready", "partial"})


def _build_backend(reading: dict[str, Any]) -> SandboxBackend | None:
    name = str(reading.get("backend") or "")
    enforcement = Enforcement.from_dict(reading.get("enforcement") or {})
    detail = dict(reading.get("detail") or {})
    try:
        if name == "bwrap":
            return BwrapBackend(enforcement=enforcement)
        if name == "wsl_bwrap":
            wsl_path = str(detail.get("wsl") or "").strip()
            distro = str(detail.get("selected_distro") or "").strip()
            if not wsl_path or not distro:
                return None
            return WslBwrapBackend(
                wsl_path=wsl_path,
                distro=distro,
                enforcement=enforcement,
            )
        if name == "seatbelt":
            return SeatbeltBackend(enforcement=enforcement)
        if name == "container":
            return ContainerBackend(
                runtime=str(detail.get("runtime") or "podman"),
                image=str(detail.get("image") or "ghcr.io/negi/sandbox:latest"),
                enforcement=enforcement,
            )
    except Exception as exc:  # noqa: BLE001 - 后端构造失败按不可用处理
        logger.warning("sandbox backend construction failed for {}: {}", name, exc)
    return None


def build_sandbox_manager(reading: dict[str, Any] | None = None) -> SandboxManager | None:
    payload = dict(reading or install_state())
    if str(payload.get("state") or "") not in _USABLE_STATES:
        return None
    backend = _build_backend(payload)
    if backend is None:
        return None
    from g3ku.deployment.data_root import data_work_path

    manager = SandboxManager(
        base_dir=data_work_path("sandboxes"),
        backends=[backend],
        install_reading=payload,
    )
    if backend.name == "wsl_bwrap":
        detail = dict(payload.get("detail") or {})
        manager.configure_wsl(
            wsl_path=str(detail.get("wsl") or ""),
            distro=str(detail.get("selected_distro") or ""),
        )
    return manager


def get_sandbox_manager(*, refresh: bool = False) -> SandboxManager | None:
    """进程内单例；后端换了（重装/卸载）用 ``refresh=True`` 重建。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None and not refresh:
            return _MANAGER
        _MANAGER = build_sandbox_manager(install_state(refresh=refresh))
        return _MANAGER


def reset_sandbox_manager_for_tests() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None


__all__ = [
    "build_sandbox_manager",
    "get_sandbox_manager",
    "reset_sandbox_manager_for_tests",
]
