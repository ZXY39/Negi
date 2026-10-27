"""沙箱运行时的安装/卸载（P1b）。

原则（与计划一致）：

* **先确认再动手**：`start_install(confirm=True)` 才执行；计划接口只读。
* **绝不静默提权**：Linux 用 `pkexec`（图形授权）或 `sudo -n`（非交互，需要密码就失败并
  把命令交还给操作员）；Windows 用 `wsl -u root`（发行版内 root 默认无密码）。
* **只装我们缺的那一件**：已 `ready` 就零步骤；已有合格发行版就不导入专有 rootfs；
  容器运行时只提示、不代装。
* **只卸我们装的**：专有发行版 `negi-sandbox` 可注销；系统 `bubblewrap` 包与用户自己的
  发行版**永不**自动卸载，只给命令。
* **卷默认保留**：卸载不动 `sandboxes/` 下的会话卷。

状态在进程内（`_STATE`）：安装可能跑几分钟，接口起线程后立刻返回，前端轮询
`GET /api/sandbox/install/status`。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from loguru import logger

from g3ku.sandbox.probe import WSL_DEDICATED_DISTRO, probe_install_state
from g3ku.sandbox.state import refresh_install_state

_ROOTFS_URL_ENV = "NEGI_SANDBOX_ROOTFS_URL"
_STEP_TIMEOUT_SECONDS = 900.0
_OUTPUT_TAIL_CHARS = 4000

_LOCK = threading.RLock()
_STATE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "action": "",
    "steps": [],
    "error": "",
    "started_at": "",
    "finished_at": "",
}


@dataclass(frozen=True, slots=True)
class InstallStep:
    id: str
    title: str
    command: list[str] = field(default_factory=list)
    elevated: bool = False
    manual: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class InstallPlan:
    platform: str
    install_state: str
    backend: str
    steps: list[InstallStep]
    notes: list[str]
    requires_elevation: bool
    may_need_reboot: bool
    uninstall_commands: list[str]
    installable: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _linux_package_manager() -> tuple[str, list[str]] | None:
    if shutil.which("apt-get"):
        return "apt-get", ["apt-get", "install", "-y", "bubblewrap"]
    if shutil.which("dnf"):
        return "dnf", ["dnf", "install", "-y", "bubblewrap"]
    if shutil.which("pacman"):
        return "pacman", ["pacman", "-S", "--noconfirm", "bubblewrap"]
    if shutil.which("zypper"):
        return "zypper", ["zypper", "--non-interactive", "install", "bubblewrap"]
    if shutil.which("apk"):
        return "apk", ["apk", "add", "bubblewrap"]
    return None


def _base_plan(*, payload: dict[str, Any], system: str) -> InstallPlan:
    return InstallPlan(
        platform=system,
        install_state=str(payload.get("state") or ""),
        backend=str(payload.get("backend") or ""),
        steps=[],
        notes=[],
        requires_elevation=False,
        may_need_reboot=False,
        uninstall_commands=[],
        installable=False,
    )


def build_install_plan(reading: dict[str, Any] | None = None) -> InstallPlan:
    payload = dict(reading or probe_install_state())
    state = str(payload.get("state") or "")
    detail = dict(payload.get("detail") or {})
    system = platform.system().lower()
    base = _base_plan(payload=payload, system=system)

    if state == "ready":
        return replace(base, notes=["沙箱运行时已就绪，无需安装。"])

    if system == "linux":
        manager = _linux_package_manager()
        if manager is None:
            return replace(
                base,
                reason="no_supported_package_manager",
                notes=["未找到受支持的包管理器；请手动安装 bubblewrap 后刷新。"],
            )
        name, command = manager
        elevated_cmd = ["pkexec", *command] if shutil.which("pkexec") else ["sudo", "-n", *command]
        return replace(
            base,
            steps=[
                InstallStep(
                    id="install-bubblewrap",
                    title="安装 bubblewrap（发行版包管理器）",
                    command=elevated_cmd,
                    elevated=True,
                    detail="约 100 KB；需要管理员授权。",
                )
            ],
            notes=["不会安装容器运行时；已装 Podman/Docker 时探测会自动优先使用。"],
            requires_elevation=True,
            uninstall_commands=[f"sudo {name} remove bubblewrap"],
            installable=True,
        )

    if system == "darwin":
        return replace(
            base,
            reason="seatbelt_unavailable",
            notes=["macOS 自带 sandbox-exec 时无需安装；当前探测不到，无法自动修复。"],
        )

    if system == "windows":
        distros = [str(item) for item in list(detail.get("distros") or [])]
        if distros:
            distro = distros[0]
            command = [
                str(detail.get("wsl") or "wsl"),
                "-u",
                "root",
                "-d",
                distro,
                "--",
                "sh",
                "-lc",
                "command -v apt-get >/dev/null && apt-get install -y bubblewrap "
                "|| command -v dnf >/dev/null && dnf install -y bubblewrap "
                "|| command -v apk >/dev/null && apk add bubblewrap",
            ]
            return replace(
                base,
                steps=[
                    InstallStep(
                        id=f"install-bubblewrap-{distro}",
                        title=f"在发行版 {distro} 里安装 bubblewrap",
                        command=command,
                        detail="以发行版 root 执行；不修改你的默认用户与 shell 配置。",
                    )
                ],
                notes=[
                    "复用已安装的发行版，不导入专有 rootfs；bubblewrap 只装在这个发行版里。",
                    f"卸载命令：wsl -d {distro} -- sudo apt-get remove bubblewrap（由你手动执行）",
                ],
                uninstall_commands=[f"wsl -d {distro} -- sudo apt-get remove bubblewrap"],
                installable=True,
            )
        rootfs_url = str(os.getenv(_ROOTFS_URL_ENV, "")).strip()
        if rootfs_url:
            return replace(
                base,
                steps=[
                    InstallStep(
                        id="import-rootfs",
                        title=f"导入专有发行版 {WSL_DEDICATED_DISTRO}",
                        command=[],
                        detail=f"从 {rootfs_url} 下载并 wsl --import（本版本未内置下载器，需离线导入）。",
                        manual=True,
                    )
                ],
                reason="rootfs_import_manual",
                notes=["专有 rootfs 需要离线导入；当前配置只给出目标发行版名，不代下载。"],
                uninstall_commands=[f"wsl --unregister {WSL_DEDICATED_DISTRO}"],
                installable=False,
            )
        return replace(
            base,
            reason="wsl_distro_missing",
            notes=[
                "这台机器还没有任何 WSL 发行版。先装一个（例如 `wsl --install -d Ubuntu`），"
                "再回来点安装；本项目会复用它，不强制专有发行版。",
            ],
            installable=False,
        )

    return replace(
        base,
        reason="unsupported_platform",
        notes=["当前平台没有可自动安装的后端。"],
    )


def _run_step(step: InstallStep) -> dict[str, Any]:
    started = time.time()
    if step.manual or not step.command:
        return {"id": step.id, "status": "manual", "output": step.detail, "seconds": 0.0}
    try:
        completed = subprocess.run(  # noqa: S603 - argv 由计划生成，不拼 shell
            step.command,
            capture_output=True,
            timeout=_STEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "id": step.id,
            "status": "failed",
            "output": str(exc),
            "seconds": round(time.time() - started, 2),
        }
    output = ((completed.stdout or b"") + (completed.stderr or b"")).decode("utf-8", errors="replace")
    status = "ok" if completed.returncode == 0 else "failed"
    return {
        "id": step.id,
        "status": status,
        "exit_code": completed.returncode,
        "output": output[-_OUTPUT_TAIL_CHARS:],
        "seconds": round(time.time() - started, 2),
    }


def _set_state(**payload: Any) -> None:
    with _LOCK:
        _STATE.update(payload)


def install_status() -> dict[str, Any]:
    with _LOCK:
        return {
            "running": bool(_STATE["running"]),
            "phase": str(_STATE["phase"]),
            "action": str(_STATE["action"]),
            "steps": [dict(item) for item in list(_STATE["steps"])],
            "error": str(_STATE["error"]),
            "started_at": str(_STATE["started_at"]),
            "finished_at": str(_STATE["finished_at"]),
        }


def _run_install(plan: InstallPlan) -> None:
    results: list[dict[str, Any]] = []
    error = ""
    try:
        for step in plan.steps:
            _set_state(phase=f"running:{step.id}")
            result = _run_step(step)
            results.append(result)
            _set_state(steps=list(results))
            if result["status"] == "failed":
                error = f"step {step.id} failed"
                break
    except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常丢掉
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("sandbox install failed: {}", exc)
    finally:
        reading = refresh_install_state()
        _set_state(
            running=False,
            phase="failed" if error else "done",
            error=error,
            steps=list(results),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        logger.info("sandbox install finished: state={}", reading.get("state"))


def start_install(*, confirm: bool) -> dict[str, Any]:
    if not confirm:
        return {"ok": False, "detail": "install_confirmation_required"}
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "detail": "install_already_running"}
        plan = build_install_plan()
        if not plan.installable or not plan.steps:
            return {"ok": False, "detail": plan.reason or "nothing_to_install", "plan": plan.to_dict()}
        _STATE.update(
            {
                "running": True,
                "phase": "starting",
                "action": "install",
                "steps": [],
                "error": "",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "finished_at": "",
            }
        )
    thread = threading.Thread(target=_run_install, args=(plan,), name="sandbox-install", daemon=True)
    thread.start()
    return {"ok": True, "started": True, "plan": plan.to_dict()}


def start_uninstall(*, confirm: bool) -> dict[str, Any]:
    """只注销我们导入的专有发行版；系统包与用户发行版永不自动卸载。"""
    if not confirm:
        return {"ok": False, "detail": "uninstall_confirmation_required"}
    plan = build_install_plan()
    commands = [
        item
        for item in plan.uninstall_commands
        if item.startswith(f"wsl --unregister {WSL_DEDICATED_DISTRO}")
    ]
    if not commands:
        return {
            "ok": False,
            "detail": "nothing_we_installed",
            "message": "没有本项目安装的内容可卸载；系统包与你的发行版请手动处理。",
            "manual_commands": list(plan.uninstall_commands),
        }
    results = []
    for command in commands:
        argv = command.split()
        step = InstallStep(id="unregister", title=command, command=argv)
        results.append({"command": command, **_run_step(step)})
    reading = refresh_install_state()
    return {
        "ok": True,
        "results": results,
        "install_state": str(reading.get("state") or ""),
        "note": "会话卷保留在数据根 sandboxes/ 下，需要先导出再删。",
    }


def reset_install_state_for_tests() -> None:
    with _LOCK:
        _STATE.update(
            {
                "running": False,
                "phase": "idle",
                "action": "",
                "steps": [],
                "error": "",
                "started_at": "",
                "finished_at": "",
            }
        )


__all__ = [
    "InstallPlan",
    "InstallStep",
    "build_install_plan",
    "install_status",
    "reset_install_state_for_tests",
    "start_install",
    "start_uninstall",
]
