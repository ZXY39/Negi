"""开机自启：把"登录后拉起 Negi"这条钩子登记在操作系统侧。

事实源是操作系统自己的登记（Run 键 / systemd unit / LaunchAgent），不是配置字段：
勾选框读的就是它，因此不会出现"配置说开着、系统里根本没有"这类两套账。

三条边界：

* 落在哪一层由登录会话决定。Windows 写当前用户的 ``HKCU\\...\\Run``，Linux 用
  ``systemctl --user enable``，macOS 用 LaunchAgent —— 都是"这个人登录后起"，
  不是"没人登录也起"。后者需要管理员权限或服务账户，不在这个钩子的范围内。
* 容器里没有这件事：``docker/*-entrypoint.sh`` 已经把 web 与 worker 当主进程跑，
  重启由容器策略负责。这里报 ``supported=False`` 并说明原因，绝不假装成功。
* 起服务走 ``negi_bootstrap.py start --no-browser``，与桌面图标同一条链：端口已有人在
  应答就只探活不重开，所以自启不会把正在工作的实例收割重启；``--no-browser`` 让登录时
  不弹浏览器页签。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

RUNTIME_ROOT = Path(__file__).resolve().parents[2]

# 与 `install.ps1` 写的快捷方式同名：一处改名两处都要动，界面与开始菜单里它是同一个应用。
APP_NAME = "Negi"

WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SYSTEMD_UNIT_NAME = "negi.service"
LAUNCHD_LABEL = "com.negi.web"

_BACKEND_WINDOWS = "windows"
_BACKEND_SYSTEMD = "systemd-user"
_BACKEND_LAUNCHD = "launchd"
_BACKEND_NONE = "none"

# 探测登记面要花一次 systemctl 往返，而 /api/bootstrap/status 是被前端轮询的；
# 这台机器 offering 哪个登记面在本进程生命周期内不会变，所以只算一次。
# 「登记在不在」不缓存：操作员可能随时自己删掉那条 Run 值。
_BACKEND_CACHE: dict[str, str] = {}

_UNAVAILABLE_REASON = "这台机器没有可用的开机自启登记面。"
_CONTAINER_REASON = "容器里的 web 与 worker 由入口脚本和容器重启策略负责，不在系统里登记自启。"


def container_detected() -> bool:
    """Whether this process runs inside a container image managed elsewhere."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(token in cgroup for token in ("docker", "kubepods", "containerd", "lxc"))


def launcher_command(root: Path | None = None) -> list[str]:
    """The command an autostart登记 must run: the `start` lane, without a browser."""
    root = root or RUNTIME_ROOT
    if os.name == "nt":
        interpreter = root / ".venv" / "Scripts" / "pythonw.exe"
        if not interpreter.is_file():
            interpreter = Path(sys.executable)
    else:
        interpreter = root / ".venv" / "bin" / "python"
        if not interpreter.is_file():
            interpreter = Path(sys.executable)
    return [str(interpreter), str(root / "negi_bootstrap.py"), "start", "--no-browser"]


def _windows_command_string(argv: list[str]) -> str:
    return " ".join(f'"{item}"' for item in argv)


def _backend_name() -> str:
    if _BACKEND_CACHE:
        return _BACKEND_CACHE["backend"]
    resolved = _detect_backend()
    _BACKEND_CACHE["backend"] = resolved
    return resolved


def reset_backend_cache() -> None:
    """Forget the probed登记面; tests and a changed environment call this."""
    _BACKEND_CACHE.clear()


def _detect_backend() -> str:
    if container_detected():
        return _BACKEND_NONE
    system = platform.system().lower()
    if system == "windows":
        return _BACKEND_WINDOWS
    if system == "darwin":
        return _BACKEND_LAUNCHD
    if shutil.which("systemctl") and _systemd_user_reachable():
        return _BACKEND_SYSTEMD
    return _BACKEND_NONE


def _systemd_user_reachable() -> bool:
    return _run(["systemctl", "--user", "show-environment"]) == 0


def _run(argv: list[str]) -> int:
    """Return the exit code of a short helper command, or 1 when it cannot run."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=20,
            text=True,
            encoding="utf-8",
            errors="ignore",
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return int(completed.returncode or 0)


def _no_window_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
    }


class _WindowsRegistryBackend:
    """HKCU Run value: no elevation, per-user, applied at next logon."""

    backend = _BACKEND_WINDOWS

    def __init__(self, winreg_module: Any | None = None) -> None:
        self._winreg = winreg_module

    @property
    def _module(self) -> Any:
        if self._winreg is not None:
            return self._winreg
        import winreg  # noqa: PLC0415 - windows-only, kept off the import path elsewhere

        return winreg

    def read(self) -> str:
        module = self._module
        try:
            with module.OpenKey(module.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, module.KEY_READ) as key:
                value, _ = module.QueryValueEx(key, APP_NAME)
        except FileNotFoundError:
            # 只有"这条值不存在"才是没开。策略挡住、句柄给不成都不是答案的一部分，
            # 让它们抛出去，由 describe 说成读不动，而不是替操作员宣布已关闭。
            return ""
        return str(value or "")

    def install(self, command: list[str]) -> None:
        module = self._module
        data = _windows_command_string(command)
        with module.OpenKey(module.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, module.KEY_SET_VALUE) as key:
            module.SetValueEx(key, APP_NAME, 0, module.REG_SZ, data)

    def uninstall(self) -> None:
        module = self._module
        try:
            with module.OpenKey(module.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, module.KEY_SET_VALUE) as key:
                module.DeleteValue(key, APP_NAME)
        except OSError:
            # 没有这条登记就是已经关掉，不把它当失败。
            pass

    def is_installed(self, command: list[str]) -> bool:
        return self.read().strip().lower() == _windows_command_string(command).strip().lower()


class _SystemdUserBackend:
    """A user unit under ``~/.config/systemd/user`` enabled for the login target."""

    backend = _BACKEND_SYSTEMD

    @property
    def unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME

    def unit_text(self, command: list[str]) -> str:
        root = RUNTIME_ROOT
        argv = " ".join(_quote(item) for item in command)
        return (
            "[Unit]\n"
            "Description=Negi agent runtime\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "RemainAfterExit=no\n"
            f"WorkingDirectory={_quote(str(root))}\n"
            f"ExecStart={argv}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def install(self, command: list[str]) -> None:
        path = self.unit_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.unit_text(command), encoding="utf-8")
        if _run(["systemctl", "--user", "daemon-reload"]) != 0:
            raise RuntimeError("systemctl --user daemon-reload 失败。")
        if _run(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME]) != 0:
            raise RuntimeError(f"systemctl --user enable {SYSTEMD_UNIT_NAME} 失败。")

    def uninstall(self) -> None:
        _run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME])
        try:
            self.unit_path.unlink()
        except OSError:
            pass
        _run(["systemctl", "--user", "daemon-reload"])

    def is_installed(self, command: list[str]) -> bool:
        if not self.unit_path.is_file():
            return False
        return _run(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME]) == 0


class _LaunchdBackend:
    """A LaunchAgent plist with ``RunAtLoad``: loaded for this user's GUI session."""

    backend = _BACKEND_LAUNCHD

    @property
    def plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    def install(self, command: list[str]) -> None:
        path = self.plist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_plist_text(command, RUNTIME_ROOT), encoding="utf-8")
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
        if _run(["launchctl", "bootstrap", domain, str(path)]) != 0:
            raise RuntimeError("launchctl bootstrap 失败。")

    def uninstall(self) -> None:
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
        _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        try:
            self.plist_path.unlink()
        except OSError:
            pass

    def is_installed(self, command: list[str]) -> bool:
        if not self.plist_path.is_file():
            return False
        return _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]) == 0


def _quote(value: str) -> str:
    return f'"{value}"' if any(token in value for token in (" ", "\t")) else value


def _plist_text(command: list[str], root: Path) -> str:
    arguments = "\n".join(f"        <string>{_xml_escape(item)}</string>" for item in command)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        f"    <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{arguments}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key><true/>\n"
        f"    <key>WorkingDirectory</key><string>{_xml_escape(str(root))}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_backend(backend: str, winreg_module: Any | None) -> Any:
    if backend == _BACKEND_WINDOWS:
        return _WindowsRegistryBackend(winreg_module)
    if backend == _BACKEND_SYSTEMD:
        return _SystemdUserBackend()
    if backend == _BACKEND_LAUNCHD:
        return _LaunchdBackend()
    return None


def describe(
    *,
    backend: str | None = None,
    winreg_module: Any | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Current reading of the autostart登记, with the reason when there is no face for it."""
    name = backend if backend is not None else _backend_name()
    argv = command or launcher_command()
    if name == _BACKEND_NONE:
        reason = _CONTAINER_REASON if container_detected() else _UNAVAILABLE_REASON
        return {
            "supported": False,
            "backend": _BACKEND_NONE,
            "installed": False,
            "command": list(argv),
            "reason": reason,
        }
    adapter = _build_backend(name, winreg_module)
    try:
        installed = bool(adapter.is_installed(argv))
        reason = ""
    except OSError as exc:
        # 读不动登记（注册表策略、只读家目录）要照实说出来，不能退成"已关闭"。
        installed = False
        reason = f"读取自启登记失败：{exc}"
    return {
        "supported": True,
        "backend": name,
        "installed": installed,
        "command": list(argv),
        "reason": reason,
    }


def apply_enabled(
    enabled: bool,
    *,
    backend: str | None = None,
    winreg_module: Any | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Install or remove the登记, then re-read it so the caller sees the fact, not the intent."""
    name = backend if backend is not None else _backend_name()
    argv = command or launcher_command()
    if name == _BACKEND_NONE:
        state = describe(backend=name, command=argv)
        state["ok"] = False
        return state
    adapter = _build_backend(name, winreg_module)
    try:
        if enabled:
            adapter.install(argv)
        else:
            adapter.uninstall()
        error = ""
    except Exception as exc:  # noqa: BLE001 - 操作员要看见失败原因，不能让登记动作抛穿接口
        error = str(exc) or type(exc).__name__
    state = describe(backend=name, winreg_module=winreg_module, command=argv)
    state["ok"] = not error
    if error:
        state["reason"] = error
    return state


def ensure_enabled_on_fresh_install(describer: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Installer hook: turn autostart on unless something already owns the登记.

    Only a fresh install calls this. An upgrade never touches the登记 behind the operator's
    back, and an existing登记 (possibly with another command shape) is left alone rather than
    rewritten mid-upgrade.
    """
    state = (describer or describe)()
    if not state.get("supported") or state.get("installed"):
        return state
    return apply_enabled(True)
