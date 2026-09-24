"""数据根解析：把体积数据（任务库、会话转录、日志、临时工作区）与安装目录分开。

安装目录一侧只留下代码、`.g3ku/config.json`、密钥信封（`llm-config/`、`secret-realms/`）、
资源锁与 `skills/`/`tools/` 种子；其余状态落在 `data_root()` 下的同名相对路径里。

默认解析结果等于进程 cwd，也就是历史路径本身，因此存量安装不需要迁移。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATA_DIR_ENV = "G3KU_DATA_DIR"
G3KU_DIR_NAME = ".g3ku"
POINTER_FILENAME = "data-root.json"
POINTER_VERSION = 1

SOURCE_ENV = "env"
SOURCE_POINTER = "pointer"
SOURCE_DEFAULT = "default"

_cached_root: Path | None = None
_cached_source: str = ""


class DataRootError(ValueError):
    """数据目录候选被拒；`code` 是给管理面显示的稳定标识。"""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def _absolute(raw: str | Path) -> Path:
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def config_dir() -> Path:
    """安装目录一侧的 `.g3ku`：配置、密钥信封、启动锁。"""
    return Path.cwd() / G3KU_DIR_NAME


def pointer_path() -> Path:
    return config_dir() / POINTER_FILENAME


def pointer_value() -> str:
    try:
        raw = json.loads(pointer_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("data_dir") or "").strip()


def data_root_source() -> str:
    _resolve_once()
    return _cached_source


def data_root(*, refresh: bool = False) -> Path:
    _resolve_once(force=refresh)
    assert _cached_root is not None
    return _cached_root


def _resolve_once(*, force: bool = False) -> None:
    global _cached_root, _cached_source
    if _cached_root is not None and not force:
        return
    env_value = str(os.getenv(DATA_DIR_ENV, "") or "").strip()
    if env_value:
        _cached_root, _cached_source = _absolute(env_value), SOURCE_ENV
        return
    pointer_value_text = pointer_value()
    if pointer_value_text:
        _cached_root, _cached_source = _absolute(pointer_value_text), SOURCE_POINTER
        return
    _cached_root, _cached_source = Path.cwd(), SOURCE_DEFAULT


def reset_cache() -> None:
    global _cached_root, _cached_source
    _cached_root, _cached_source = None, ""


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_g3ku_path(*parts: str, create: bool = False) -> Path:
    """数据根一侧的 `.g3ku/<parts>`，体积型运行态都挂这里。"""
    path = data_root() / G3KU_DIR_NAME
    for part in parts:
        path = path / part
    return ensure_dir(path) if create else path


def data_work_path(*parts: str, create: bool = False) -> Path:
    """数据根一侧的工作区目录（`temp/`、`sessions/`、`memory/`、`output/`）。"""
    path = data_root()
    for part in parts:
        path = path / part
    return ensure_dir(path) if create else path


def resolve_data_path(raw: str | Path | None, *, default: str = "") -> Path:
    """存储串解析：相对值挂到数据根，绝对值原样保留。"""
    text = str(raw or "").strip() or str(default or "").strip()
    if not text:
        raise DataRootError("storage_path_empty", "存储路径配置为空")
    path = Path(text).expanduser()
    if path.is_absolute():
        return Path(os.path.normpath(str(path)))
    return data_root() / path


def describe_data_root() -> dict[str, Any]:
    root = data_root()
    return {
        "data_root": str(root),
        "source": data_root_source(),
        "default_root": str(Path.cwd()),
        "is_default": root == Path.cwd(),
        "pointer_path": str(pointer_path()),
        "env_var": DATA_DIR_ENV,
    }


def validate_data_root(raw: str | Path) -> Path:
    """校验候选数据目录，必要时建目录并实测可写；失败抛 `DataRootError`。"""
    text = str(raw or "").strip()
    if not text:
        raise DataRootError("data_root_empty", "数据目录不能为空")
    if "\x00" in text:
        raise DataRootError("data_root_invalid", "数据目录包含非法字符")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise DataRootError("data_root_relative", "数据目录必须是绝对路径")
    target = Path(os.path.normpath(str(candidate)))

    cwd = Path.cwd()
    if target != cwd and cwd.is_relative_to(target):
        raise DataRootError("data_root_contains_install", "数据目录不能是安装目录的上层目录")
    config = config_dir()
    if target == config or target.is_relative_to(config):
        raise DataRootError("data_root_inside_config", "数据目录不能放在 .g3ku 配置目录内部")
    if target.exists() and not target.is_dir():
        raise DataRootError("data_root_is_file", "数据目录路径已存在同名文件")

    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".g3ku-write-probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise DataRootError("data_root_unwritable", f"数据目录不可写：{exc}") from exc
    return target


def write_data_root_pointer(raw: str | Path) -> dict[str, Any]:
    """记录数据目录选择；进程内缓存随之失效。"""
    target = validate_data_root(raw)
    payload = {
        "version": POINTER_VERSION,
        "data_dir": str(target),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    pointer = pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, pointer)
    reset_cache()
    return describe_data_root()
