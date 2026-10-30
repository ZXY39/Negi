"""受限模式的会话工作目录。

未安装沙箱、会话处于 ``restricted`` 意图时，数据面不再完全只读：每个会话得到
一个独立目录，文件工具只能在这个目录里写。路径挂在数据根而不是项目根——
它是运行时产物，不是仓库内容；同一个会话键跨进程稳定，换工作区不丢。

目录名带会话键摘要，避免 ``:`` / ``/`` 规范化后的两个键撞到同一个目录。
没有自动清理车道（与 ``temp/ceo`` 同纪律）：删除会话带走的是转录/快照，
不包含这里，需要时由操作员手动清理。
"""

from __future__ import annotations

import hashlib
from typing import Any

from g3ku.deployment.data_root import data_work_path
from g3ku.utils.helpers import safe_filename

SESSION_WORKSPACE_ROOT_PARTS: tuple[str, ...] = ("session-workspaces",)
_FALLBACK_NAME = "shared"


def ceo_session_workspace_dir(session_key: Any, *, create: bool = False) -> str:
    """返回 ``<数据根>/session-workspaces/<safe-key>-<digest>`` 的绝对路径。

    ``create=False`` 只解析路径；调用方真正要用它作为 cwd / 写根时传 True。
    """
    raw = str(session_key or "").strip()
    safe_session = safe_filename(raw.replace(":", "_")).strip("._")[:80]
    if not safe_session:
        safe_session = _FALLBACK_NAME
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8] if raw else "00000000"
    path = data_work_path(*SESSION_WORKSPACE_ROOT_PARTS) / f"{safe_session}-{digest}"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


__all__ = ["SESSION_WORKSPACE_ROOT_PARTS", "ceo_session_workspace_dir"]
