"""会话级临时目录解析（CEO 前门 lane）。

背景：CEO 会话此前没有任务级 ``task_temp_dir``，模型通过 ``exec`` 重定向落盘的
临时文件会散落在工作区根目录。本模块提供统一的会话级临时目录
``<数据根>/temp/ceo/<safe_session_key>``，供运行时上下文注入
（``task_temp_dir``）与运行时工具契约（``session_temp_dir``）共用。

临时内容一律挂在数据根（``g3ku.deployment.data_root``），不挂项目根：项目根是
模型交付物与仓库相对路径的锚，也是 skills/tools 的资源根，会话中间产物不该留在那里。
``temp/tasks/`` 早就是同一个根，本模块与之对齐后两条 temp 车道同形。

目录本身惰性创建：``exec`` 在把它用作默认 cwd 时会 mkdir，
``filesystem_write`` 在写文件时会创建父目录，这里只负责解析路径。
"""

from __future__ import annotations

from typing import Any

from g3ku.deployment.data_root import data_work_path
from g3ku.utils.helpers import safe_filename

CEO_SESSION_TEMP_ROOT_PARTS: tuple[str, ...] = ('temp', 'ceo')
CEO_SESSION_TEMP_FALLBACK_NAME = 'shared'


def ceo_session_temp_dir(session_key: Any) -> str:
    """返回 ``<数据根>/temp/ceo/<safe_session_key>`` 绝对路径。

    - ``session_key`` 形如 ``web:ceo-57836448a1d7``，冒号等不安全字符会被
      ``safe_filename`` 规范化（与 ``sessions/`` 落盘文件名口径一致）。
    - 无法解析会话键时按 ``shared`` 落点，保持与旧行为一致。
    """
    root = data_work_path(*CEO_SESSION_TEMP_ROOT_PARTS)
    safe_session = safe_filename(str(session_key or '').replace(':', '_')).strip('._') or ''
    if not safe_session:
        safe_session = CEO_SESSION_TEMP_FALLBACK_NAME
    return str(root / safe_session)
