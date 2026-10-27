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
# 我们自己的 WSL 发行版名字：不是"必须"，而是"优先 + 保证项"。装箱脚本与探测共用。
WSL_DEDICATED_DISTRO = "negi-sandbox"
# 逐发行版探测要起一次 `wsl` 互操作调用；机器上装了七八个发行版时不能全扫。
_WSL_MAX_DISTRO_CHECKS = 4

# 一个发行版能否承载 jail 的三条硬条件里的两条（第三条"不依赖发行版 dotfile"
# 由 profile 保证：jail 只绑最小根，不读发行版的家目录与 shell 配置）。
# bwrap 缺失时给可读的安装提示；python 缺失时 fsop 跑不起来，判不合格。
_WSL_CAPABILITY_SCRIPT = (
    'b="$(command -v bwrap || true)"; '
    'p="$(command -v python3 || command -v python || true)"; '
    'i=""; if [ -e /proc/sys/fs/binfmt_misc/WSLInterop ]; then i=1; fi; '
    'printf "%s|%s|%s" "$b" "$p" "$i"'
)


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


def _wsl_list_distros(wsl: str) -> tuple[bool, list[str], dict[str, Any]]:
    """列出已安装发行版。`wsl -l -q` 的输出是 UTF-16LE（带 BOM）。"""
    code, out = _run([wsl, "-l", "-q"], encoding="utf-16-le")
    distros: list[str] = []
    for line in out.replace("\x00", "").splitlines():
        name = line.strip().lstrip("\ufeff").strip()
        if name and name not in distros:
            distros.append(name)
    return code == 0, distros, {"rc": code}


def _wsl_distro_capabilities(wsl: str, distro: str) -> dict[str, Any]:
    # `-e`, not `--`: with `--` wsl.exe joins argv and re-evaluates it through the
    # distro's default shell, so the `-lc` script loses its quoting and every `$b`
    # in it expands to empty in the outer shell — the probe then reads a qualified
    # distro as having no bwrap and no python3.
    code, out = _run([wsl, "-d", distro, "-e", "sh", "-lc", _WSL_CAPABILITY_SCRIPT])
    line = out.strip().splitlines()[-1] if out.strip() else ""
    parts = line.split("|")
    bwrap = parts[0].strip() if len(parts) > 0 else ""
    python = parts[1].strip() if len(parts) > 1 else ""
    interop = (parts[2].strip() if len(parts) > 2 else "") == "1"
    return {
        "name": distro,
        "probe_rc": code,
        "bwrap": bwrap,
        "python": python,
        "interop": interop,
        # 三条硬条件里的两条；第三条（不读发行版 dotfile）由 profile 保证。
        "qualified": bool(bwrap) and bool(python),
    }


def _wsl_candidate_order(distros: list[str]) -> list[str]:
    """专用发行版优先，其余保持 `wsl -l` 的顺序；最多探前 N 个。"""
    ordered = [name for name in distros if name == WSL_DEDICATED_DISTRO]
    ordered += [name for name in distros if name != WSL_DEDICATED_DISTRO]
    return ordered[:_WSL_MAX_DISTRO_CHECKS]


def _wsl_ready_result(distro: str, capabilities: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    return _result(
        state="ready",
        backend=BACKEND_WSL_BWRAP,
        detail={
            **detail,
            "selected_distro": distro,
            "dedicated": distro == WSL_DEDICATED_DISTRO,
            "bwrap": str(capabilities.get("bwrap") or ""),
            "python": str(capabilities.get("python") or ""),
            # interop 注册存在不是不合格项：profile 用 --unshare-pid + 全新 /proc
            # 把它挡在 jail 外。这里只把它作为 doctor 的读数列出来。
            "wsl_interop": bool(capabilities.get("interop")),
        },
        enforcement={
            "fs_read": FULL,
            "fs_write": FULL,
            "network": FULL,
            "process": FULL,
            # WSL2 的 cgroup 支持随发行版与 systemd 配置浮动，没有实测就不报 full。
            "resource": PARTIAL,
        },
    )


def _probe_windows() -> dict[str, Any]:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    detail: dict[str, Any] = {"wsl": wsl or "", "dedicated_distro": WSL_DEDICATED_DISTRO}
    distro_failure: dict[str, Any] | None = None
    if wsl:
        listed, distros, listing = _wsl_list_distros(wsl)
        detail["distros_rc"] = listing.get("rc")
        detail["distros"] = distros
        if listed and distros:
            checked: list[dict[str, Any]] = []
            best_missing_bwrap: dict[str, Any] | None = None
            for distro in _wsl_candidate_order(distros):
                capabilities = _wsl_distro_capabilities(wsl, distro)
                checked.append(
                    {
                        "name": distro,
                        "bwrap": bool(capabilities.get("bwrap")),
                        "python": bool(capabilities.get("python")),
                        "qualified": bool(capabilities.get("qualified")),
                    }
                )
                if capabilities["qualified"]:
                    detail["checked"] = checked
                    return _wsl_ready_result(distro, capabilities, detail)
                if capabilities.get("python") and best_missing_bwrap is None:
                    best_missing_bwrap = capabilities
            detail["checked"] = checked
            if best_missing_bwrap is not None:
                # 有 Python（fsop 能跑）但缺 bwrap：给出发行版内的安装命令，不静默降级。
                detail["install_hint"] = (
                    f"wsl -d {best_missing_bwrap['name']} -- sudo apt-get install -y bubblewrap"
                )
                distro_failure = _result(
                    state="runtime_missing",
                    reason="bwrap_missing_in_distro",
                    detail=detail,
                )
            else:
                distro_failure = _result(
                    state="runtime_missing",
                    reason="no_qualified_distro",
                    detail=detail,
                )
    # 发行版不合格时仍然看容器：一台装着 Docker 的机器不该因为 WSL 里缺 bwrap 被判不可用。
    container = _container_backend()
    if container is not None:
        return container
    if distro_failure is not None:
        return distro_failure
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
    "WSL_DEDICATED_DISTRO",
    "probe_install_state",
    "third_party_usable",
]
