"""``/shared/{inbox,outbox}`` 文件桥：人工导入导出。

只有这两条通道能让文件进出 jail，绝不自动双向同步（Hermes 式的 sync-back 与
"除非手动操作不同步"直接冲突）。两条纪律：

* **来源与目标都过根校验**：导入只接受普通文件（symlink 一律拒），导出路径必须解析后
  仍落在对应区根内；文件名过 `safe_filename`，不允许路径分隔符。
* **大小上限**：单文件 64 MB；超过就拒绝并让操作员换用 tar 流车道（后续阶段）。

本地后端直读卷；``wsl_bwrap`` 走 ``wsl.exe`` 的 cat/tee（卷在发行版里，宿主看不到）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from g3ku.sandbox.errors import SandboxDeniedError, SandboxUnavailableError
from g3ku.sandbox.manager import SandboxHandle
from g3ku.utils.helpers import safe_filename

MAX_BRIDGE_FILE_BYTES = 64 * 1024 * 1024
AREAS = ("inbox", "outbox")


def _safe_name(value: Any, *, fallback: str) -> str:
    """文件名只留单段：分隔符被吃掉，首尾的点也去掉（`.` / `..` / 隐藏文件都不成立）。"""
    cleaned = safe_filename(str(value or "")).strip("._")
    return cleaned or fallback


def _area_root(handle: SandboxHandle, area: str) -> Path:
    normalized = str(area or "").strip().lower()
    if normalized not in AREAS:
        raise SandboxDeniedError(f"unknown shared area: {area}")
    raw = handle.paths.get(normalized) or ""
    if not raw:
        raise SandboxUnavailableError(f"sandbox area is not configured: {normalized}")
    return Path(raw)


def _wsl_argv(handle: SandboxHandle, script: str) -> list[str]:
    backend = handle.backend
    wsl_path = getattr(backend, "_wsl", "")
    distro = getattr(backend, "_distro", "")
    if not wsl_path or not distro:
        raise SandboxUnavailableError("wsl file bridge is not configured")
    return [str(wsl_path), "-d", str(distro), "--", "sh", "-lc", script]


def import_file(handle: SandboxHandle, source: Path | str, *, name: str | None = None) -> dict[str, Any]:
    path = Path(source)
    if path.is_symlink():
        raise SandboxDeniedError("importing symlinks is not allowed")
    if not path.is_file():
        raise SandboxUnavailableError(f"source is not a regular file: {source}")
    size = path.stat().st_size
    if size > MAX_BRIDGE_FILE_BYTES:
        raise SandboxDeniedError(f"file exceeds the {MAX_BRIDGE_FILE_BYTES // (1024 * 1024)} MB import limit")
    resolved_name = _safe_name(name or path.name, fallback="imported.bin")

    if handle.backend.name == "wsl_bwrap":
        target_dir = str(_area_root(handle, "inbox"))
        script = f"mkdir -p '{target_dir}' && cat > '{target_dir}/{resolved_name}'"
        completed = subprocess.run(  # noqa: S603 - 固定 argv，路径已过 safe_filename
            _wsl_argv(handle, script),
            input=path.read_bytes(),
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise SandboxUnavailableError("wsl import failed")
        return {"ok": True, "name": resolved_name, "bytes": size, "area": "inbox"}

    target = _area_root(handle, "inbox") / resolved_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    return {"ok": True, "name": resolved_name, "bytes": size, "area": "inbox", "path": str(target)}


def export_file(handle: SandboxHandle, name: str, *, area: str = "outbox") -> tuple[str, bytes]:
    """返回 ``(文件名, 字节)``；调用方负责落盘或直接回给浏览器。"""
    resolved_name = _safe_name(name, fallback="export.bin")
    root = _area_root(handle, area)

    if handle.backend.name == "wsl_bwrap":
        target = f"{root}/{resolved_name}"
        completed = subprocess.run(  # noqa: S603
            _wsl_argv(handle, f"cat '{target}'"),
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise SandboxUnavailableError(f"wsl export failed for {resolved_name}")
        payload = completed.stdout or b""
        if len(payload) > MAX_BRIDGE_FILE_BYTES:
            raise SandboxDeniedError("file exceeds the export limit")
        return resolved_name, payload

    candidate = (root / resolved_name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SandboxDeniedError(f"export path escapes the shared area: {name}") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise SandboxUnavailableError(f"no such exported file: {resolved_name}")
    size = candidate.stat().st_size
    if size > MAX_BRIDGE_FILE_BYTES:
        raise SandboxDeniedError("file exceeds the export limit")
    return resolved_name, candidate.read_bytes()


def list_area(handle: SandboxHandle, *, area: str = "outbox", max_entries: int = 200) -> list[dict[str, Any]]:
    root = _area_root(handle, area)
    if handle.backend.name == "wsl_bwrap":
        script = f"ls -1p '{root}' 2>/dev/null | head -{max(1, int(max_entries))}"
        completed = subprocess.run(  # noqa: S603
            _wsl_argv(handle, script),
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            return []
        names = (completed.stdout or b"").decode("utf-8", errors="replace").splitlines()
        return [{"name": name.rstrip("/"), "kind": "dir" if name.endswith("/") else "file"} for name in names if name]

    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if len(entries) >= max_entries:
            break
        entries.append(
            {
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else 0,
            }
        )
    return entries


__all__ = ["AREAS", "MAX_BRIDGE_FILE_BYTES", "export_file", "import_file", "list_area"]
