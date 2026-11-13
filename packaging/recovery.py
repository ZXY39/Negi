"""卡死设备的一次性更新恢复（P0-4）。

适用形态：设备被公司策略锁进 PowerShell ConstrainedLanguage（机器级 __PSLockdownPolicy=4）
且本机没有持久化的 uv——旧版本的应用内更新会在 install.ps1 的 Ensure-Uv 处必败，而旧代码
不可变，修复无法通过坏车道自动到达。这个脚本把修复一次性落地：优雅退出 → 备 uv → 跑修复版
安装器 -Upgrade → 重新拉起。之后应用内更新永久恢复。

为什么是 Python 主体：ConstrainedLanguage 只约束 PowerShell 的 .NET/COM 方法，对 Python 与
原生 exe 无影响。下载用 urllib、解压用 zipfile、git/uv 是原生 exe；唯一调 PowerShell 的地方
是跑那份**修复版** install.ps1（它自己已含 cmdlet-only 兼容车道）。薄入口 recovery.cmd 只
负责找解释器并转交。

用法（双击 recovery.cmd，或显式）：
    recovery.cmd [--root <安装根>] [--ref <vX.Y.Z>] [--port <端口>]
缺省：安装根读 %LOCALAPPDATA%/Negi/install-root.json，版本取远端最高 vX.Y.Z 标签，端口读
安装根的 .g3ku/config.json。修复版 install.ps1 优先用与本脚本同目录的那份（发布包自带），
没有就从目标 ref 的 raw 地址下载。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO_OWNER = "ZXY39"
REPO_NAME = "Negi"
UV_VERSION = "0.13.8"
UV_ZIP_BASES = (
    "https://releases.astral.sh/github/uv/releases/download",
    "https://github.com/astral-sh/uv/releases/download",
)
EXIT_WAIT_SECONDS = 90.0


def _log(message: str) -> None:
    print(f"[recovery] {message}", flush=True)


def _resolve_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    pointer = Path(os.environ.get("LOCALAPPDATA", "")) / "Negi" / "install-root.json"
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        if data.get("root"):
            return Path(str(data["root"])).resolve()
    except (OSError, ValueError):
        pass
    return (Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Negi").resolve()


def _resolve_port(root: Path, explicit: int | None) -> int:
    if explicit:
        return explicit
    try:
        data = json.loads((root / ".g3ku" / "config.json").read_text(encoding="utf-8"))
        port = int((data.get("web") or {}).get("port") or 0)
        if 0 < port < 65536:
            return port
    except (OSError, ValueError):
        pass
    return 18790


def _resolve_ref(root: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    git = shutil.which("git")
    if git and (root / ".git").exists():
        try:
            done = subprocess.run(
                [git, "-C", str(root), "ls-remote", "--tags", "origin"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            tags = []
            for line in (done.stdout or "").splitlines():
                ref = line.split("refs/tags/")[-1].strip()
                if ref.startswith("v") and ref.endswith("^{}"):
                    ref = ref[:-3]
                parts = ref.lstrip("v").split(".")
                if len(parts) == 3 and all(p.isdigit() for p in parts):
                    tags.append((tuple(int(p) for p in parts), ref))
            if tags:
                return max(tags)[1]
        except (subprocess.TimeoutExpired, OSError):
            pass
    raise SystemExit("[recovery] 取不到目标版本：请用 --ref vX.Y.Z 点名（远端不可达或没有 git）")


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _graceful_exit(port: int) -> None:
    if not _port_busy(port):
        _log(f"端口 {port} 没有服务在跑，跳过退出")
        return
    body = json.dumps({"pause_running_work": True}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/bootstrap/exit",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60.0) as response:
            _log(f"退出请求被接受（HTTP {response.status}），等服务持久化暂停")
    except Exception as exc:
        _log(f"退出请求未成功（{exc}），继续等端口释放")
    deadline = time.monotonic() + EXIT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _port_busy(port):
            time.sleep(2.0)
            _log("服务已退出")
            return
        time.sleep(1.0)
    _log(f"端口 {port} 等了 {EXIT_WAIT_SECONDS:g}s 仍占用；安装器会自行处理残留进程")


def _local_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    for rel in (".local/bin/uv.exe", ".cargo/bin/uv.exe"):
        candidate = home / rel
        if candidate.exists():
            return str(candidate)
    return None


def _ensure_uv() -> None:
    if _local_uv():
        _log(f"uv 已在：{_local_uv()}")
        return
    arch = "aarch64" if os.environ.get("PROCESSOR_ARCHITECTURE") == "ARM64" else "x86_64"
    asset = f"uv-{arch}-pc-windows-msvc.zip"
    bin_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(os.environ.get("TEMP", str(Path.home()))) / f"negi-recovery-uv-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    zip_path = tmp / asset
    ok = False
    for base in UV_ZIP_BASES:
        url = f"{base}/{UV_VERSION}/{asset}"
        try:
            _log(f"下载 uv {UV_VERSION}：{url}")
            with urllib.request.urlopen(url, timeout=120.0) as response:
                zip_path.write_bytes(response.read())
            ok = True
            break
        except Exception as exc:
            _log(f"下载失败（{exc}），试下一个源")
    if not ok:
        raise SystemExit("[recovery] 取不到 uv：恢复网络，或手动把 uv.exe 放进 " + str(bin_dir))
    with zipfile.ZipFile(zip_path) as archive:
        for name in ("uv.exe", "uvx.exe", "uvw.exe"):
            try:
                data = archive.read(name)
            except KeyError:
                continue
            (bin_dir / name).write_bytes(data)
    shutil.rmtree(tmp, ignore_errors=True)
    _log(f"uv 已放进 {bin_dir}")


def _obtain_installer(root: Path, ref: str) -> Path:
    sibling = Path(__file__).resolve().parent / "install.ps1"
    if sibling.exists():
        _log(f"用发布包自带的修复版安装器：{sibling}")
        return sibling
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{ref}/install.ps1"
    target = Path(os.environ.get("TEMP", str(root))) / f"negi-recovery-install-{ref}.ps1"
    _log(f"下载修复版安装器：{url}")
    with urllib.request.urlopen(url, timeout=60.0) as response:
        target.write_bytes(response.read())
    return target


def _run_installer(installer: Path, root: Path, ref: str) -> None:
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer),
        "-Dir", str(root), "-Upgrade", "-NoStart", "-Ref", ref,
    ]
    _log("跑修复版安装器：" + " ".join(command))
    done = subprocess.run(command, cwd=str(root))
    if done.returncode != 0:
        raise SystemExit(f"[recovery] 安装器非零退出（{done.returncode}）；看上面的输出定位")
    _log("安装器完成")


def _relaunch(root: Path, port: int) -> None:
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = root / ".venv" / "bin" / "python"
    log_file = root / ".g3ku" / "logs" / "console.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"cwd": str(root), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log_file.open("a", encoding="utf-8", errors="replace") as sink:
        kwargs["stdout"] = sink
        kwargs["stderr"] = subprocess.STDOUT
        subprocess.Popen([str(python), "-m", "g3ku", "web", "--port", str(port)], **kwargs)
    _log(f"已重新拉起 web（端口 {port}），日志在 {log_file}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recovery", description="Negi 卡死设备的一次性更新恢复")
    parser.add_argument("--root", default=None, help="安装根（缺省读安装指针）")
    parser.add_argument("--ref", default=None, help="目标版本 vX.Y.Z（缺省取远端最高标签）")
    parser.add_argument("--port", type=int, default=None, help="web 端口（缺省读配置）")
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    if not (root / "pyproject.toml").exists():
        raise SystemExit(f"[recovery] {root} 不像 Negi 安装根（没有 pyproject.toml）；用 --root 点名")
    port = _resolve_port(root, args.port)
    ref = _resolve_ref(root, args.ref)
    _log(f"安装根={root} 端口={port} 目标={ref}")

    _graceful_exit(port)
    _ensure_uv()
    installer = _obtain_installer(root, ref)
    _run_installer(installer, root, ref)
    _relaunch(root, port)
    _log("恢复完成。之后在网页点「重启并更新」即可正常升级。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
