"""会话级临时目录解析（CEO 前门 lane）。

背景：CEO 会话此前没有任务级 ``task_temp_dir``，模型通过 ``exec`` 重定向落盘的
临时文件会散落在工作区根目录。本模块提供统一的会话级临时目录
``<workspace>/temp/ceo/<safe_session_key>``，供运行时上下文注入
（``task_temp_dir``）与运行时工具契约（``session_temp_dir``）共用。

目录本身惰性创建：``exec`` 在把它用作默认 cwd 时会 mkdir，
``filesystem_write`` 在写文件时会创建父目录，这里只负责解析路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from g3ku.utils.helpers import safe_filename

CEO_SESSION_TEMP_ROOT_PARTS: tuple[str, ...] = ('temp', 'ceo')
CEO_SESSION_TEMP_FALLBACK_NAME = 'shared'


def ceo_session_temp_dir(workspace_root: Any, session_key: Any) -> str:
    """返回 ``<workspace>/temp/ceo/<safe_session_key>`` 绝对路径。

    - ``session_key`` 形如 ``web:ceo-57836448a1d7``，冒号等不安全字符会被
      ``safe_filename`` 规范化（与 ``sessions/`` 落盘文件名口径一致）。
    - 无法解析工作区或会话键时返回空字符串，调用方按"未注入"降级处理。
    """
    raw_root = str(workspace_root or '').strip()
    try:
        root = Path(raw_root).expanduser().resolve() if raw_root else Path.cwd().resolve()
    except Exception:
        return ''
    safe_session = safe_filename(str(session_key or '').replace(':', '_')).strip('._') or ''
    if not safe_session:
        safe_session = CEO_SESSION_TEMP_FALLBACK_NAME
    return str(root.joinpath(*CEO_SESSION_TEMP_ROOT_PARTS) / safe_session)
