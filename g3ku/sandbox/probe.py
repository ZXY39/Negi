"""沙箱后端的可用性探测（一次 / 进程）。

探测只回答"这台机器现在能提供哪一档执行面"，不负责安装。结论按**逐维度强制力**
上报，不是一个笼统的可用/不可用：

* `fs_read` / `fs_write` / `network` 是第三方会话的必需维度；
* `process` / `resource` 缺失时可以放行，但界面要标注。

`state` 取值与 `g3ku/sandbox/policy.py` 的安装态一致：`ready` / `partial` /
`runtime_missing` / `not_installed`。探测结果由 `state.install_state()` 缓存。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

BACKEND_BWRAP = "bwrap"
BACKEND_SEATBELT = "seatbelt"
BACKEND_LANDLOCK = "landlock"
BACKEND_WSL_BWRAP = "wsl_bwrap"
BACKEND_CONTAINER = "container"

FULL = "full"
PARTIAL = "partial"
MISSING = "missing"

REQUIRED_DIMENSIONS: tuple[str, ...] = ("fs_read", "fs_write", "network")
_DIMENSIONS: tuple[str, ...] = ("fs_read", "fs_write", "network", "process", "resource")

_PROBE_TIMEOUT_SECONDS = 8.0
# 我们自己的 WSL 发行版名字；装箱脚本与探测共用同一个常量。
WSL_DISTRO_NAME = "negi-sandbox"


def _empty_enforcement() -> dict[str, str]:
    return {dimension: MISSING for dimension in _DIMENSIONS}


def _result(
    *,
    state: str,
    backend: str = "",
    reason: str = "",
    detail: dict[str, Any] | None = None,
    enforcement: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "backend": backend,
        "reason": reason,
        "enforcement": dict(enforcement or _empty_enforcement()),
        "detail": dict(detail or {}),
        "platform": platform.system().lower(),
    }


def _run(argv: list[str], *, encoding: str = "utf-8") -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
            argv,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    raw = (completed.stdout or completed.stderr or b"")
    try:
        text = raw.decode(encoding, errors="replace")
    except (LookupError, AttributeError):
        text = ""
    return int(completed.returncode), text


def _container_backend() -> dict[str, Any] | None:
    for name in ("podman", "docker"):
        binary = shutil.which(name)
        if not binary:
            continue
        code, _ = _run([binary, "info", "--format", "{{.ServerVersion}}"])
        if code != 0:
            continue
        # 容器后端在四个维度上都拿得到，资源维度靠 cgroup flag。
        return _result(
            state="ready",
            backend=BACKEND_CONTAINER,
            detail={"runtime": name, "binary": binary},
            enforcement={
                "fs_read": FULL,
                "fs_write": FULL,
                "network": FULL,
                "process": FULL,
                "resource": FULL,
            },
        )
    return None


def _probe_linux() -> dict[str, Any]:
    bwrap = shutil.which("bwrap")
    if bwrap:
        code, out = _run([bwrap, "--version"])
        if code == 0:
            userns_ok = True
            clone_path = Path("/proc/sys/kernel/unprivileged_userns_clone")
            if clone_path.exists():
                try:
                    userns_ok = clone_path.read_text(encoding="utf-8").strip() != "0"
                except OSError:
                    userns_ok = True
            if not userns_ok:
                return _result(
                    state="runtime_missing",
                    backend=BACKEND_BWRAP,
                    reason="unprivileged_userns_disabled",
                    detail={"version": out.strip()},
                )
            # cgroup v2 决定资源维度能否拿到；拿不到判 partial（第三方必需维度仍齐全）。
            cgroup_v2 = Path("/sys/fs/cgroup/cgroup.controllers").exists()
            return _result(
                state="ready",
                backend=BACKEND_BWRAP,
                detail={"version": out.strip(), "cgroup_v2": cgroup_v2},
                enforcement={
                    "fs_read": FULL,
                    "fs_write": FULL,
                    "network": FULL,
                    "process": FULL,
                    "resource": FULL if cgroup_v2 else PARTIAL,
                },
            )
    container = _container_backend()
    if container is not None:
        return container
    return _result(
        state="not_installed",
        reason="bwrap_missing",
        detail={"hint": "install bubblewrap, or provide a container runtime"},
    )


def _probe_darwin() -> dict[str, Any]:
    seatbelt = shutil.which("sandbox-exec")
    if seatbelt:
        return _result(
            state="ready",
            backend=BACKEND_SEATBELT,
            detail={"binary": seatbelt, "deprecated": True},
            enforcement={
                "fs_read": FULL,
                "fs_write": FULL,
                "network": FULL,
                "process": MISSING,
                "resource": MISSING,
            },
        )
    container = _container_backend()
    if container is not None:
        return container
    return _result(state="not_installed", reason="seatbelt_missing")


def _probe_windows() -> dict[str, Any]:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    detail: dict[str, Any] = {"wsl": wsl or "", "distro": WSL_DISTRO_NAME}
    if wsl:
        # wsl.exe 的输出是 UTF-16LE；列发行版的 rc 在"没有已安装发行版"时为非零。
        code, out = _run([wsl, "-l", "-q"], encoding="utf-16-le")
        detail["distros_rc"] = code
        distros = [
            line.strip()
            for line in out.replace("\x00", "").splitlines()
            if line.strip() and not line.strip().startswith("\ufeff")
        ]
        detail["distros"] = distros
        if code == 0 and WSL_DISTRO_NAME in distros:
            code_inner, inner = _run(
                [wsl, "-d", WSL_DISTRO_NAME, "--", "bwrap", "--version"],
                encoding="utf-8",
            )
            if code_inner == 0:
                return _result(
                    state="ready",
                    backend=BACKEND_WSL_BWRAP,
                    detail={**detail, "bwrap": inner.strip()},
                    enforcement={
                        "fs_read": FULL,
                        "fs_write": FULL,
                        "network": FULL,
                        "process": FULL,
                        "resource": PARTIAL,
                    },
                )
            detail["bwrap_probe_rc"] = code_inner
    container = _container_backend()
    if container is not None:
        return container
    state = "runtime_missing" if wsl else "not_installed"
    return _result(
        state=state,
        reason="wsl_distro_missing",
        detail=detail,
    )


def probe_install_state() -> dict[str, Any]:
    system = platform.system().lower()
    if system == "linux":
        return _probe_linux()
    if system == "darwin":
        return _probe_darwin()
    if system == "windows":
        return _probe_windows()
    return _result(state="not_installed", reason="unsupported_platform", detail={"platform": system})


def third_party_usable(reading: dict[str, Any]) -> bool:
    """第三方会话能否进 jail：必需维度全 `full` 才算。"""
    enforcement = reading.get("enforcement") or {}
    return all(str(enforcement.get(dimension) or MISSING) == FULL for dimension in REQUIRED_DIMENSIONS)


__all__ = [
    "BACKEND_BWRAP",
    "BACKEND_CONTAINER",
    "BACKEND_LANDLOCK",
    "BACKEND_SEATBELT",
    "BACKEND_WSL_BWRAP",
    "FULL",
    "MISSING",
    "PARTIAL",
    "REQUIRED_DIMENSIONS",
    "WSL_DISTRO_NAME",
    "probe_install_state",
    "third_party_usable",
]
