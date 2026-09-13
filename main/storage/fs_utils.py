"""文件系统硬删工具（只读文件强删）。

背景：2026-09-11 存储审计发现全部硬删链路均为 ``shutil.rmtree(ignore_errors=True)``
且无只读文件处理——Windows 上 git 克隆（.git/objects 等）的只读文件会导致整棵
目录树删除失败且被静默吞掉（实例：temp/tasks 残留约 1 GB / 6 万文件）。

本模块提供带只读位修复的删除入口 ``remove_tree``：

1. 失败条目先 ``os.chmod`` 去只读位后重试一次；
2. Windows 下自动加 ``\\\\?\\`` 扩展长度前缀——超过 MAX_PATH（260 字符）的
   git 克隆深树对 Win32 API 不可见，表现为"目录非空却枚举不到子项"的
   删不掉目录壳；
3. 仍有残留时 ``logger.warning`` 列出残留路径并返回 ``False``——绝不静默失败
   （静默残留正是磁盘累积写满事故的根因之一）。

所有"必须可靠回收物理目录"的路径（终态清理、delete_task、任务文件/artifact
目录删除）应统一走本函数。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Callable

from loguru import logger

__all__ = ['remove_tree']

_MAX_REPORTED_LEFTOVERS = 5
_WIN_EXTENDED_PREFIX = '\\\\?\\'


def _extended_length_path(target: Path) -> Path:
    """Windows 下加 ``\\\\?\\`` 扩展长度前缀，绕过 260 字符 MAX_PATH 限制。

    git 克隆的深层目录树（node_modules / 测试 fixture 等）经常超长，
    超限路径对 Win32 API 不可见——rmtree 报"目录非空"却枚举不到子项，
    留下删不掉的目录壳。前缀要求绝对路径且不做规范化，其余平台原样返回。
    """
    if os.name != 'nt':
        return target
    text = str(target)
    if text.startswith(_WIN_EXTENDED_PREFIX):
        return target
    try:
        absolute = str(Path(os.path.abspath(text)))
    except (OSError, ValueError):
        return target
    if absolute.startswith(_WIN_EXTENDED_PREFIX):
        return Path(absolute)
    return Path(_WIN_EXTENDED_PREFIX + absolute)


def _retry_with_write_bit(func: Callable[[str], Any], path: str, _exc: Any) -> None:
    """rmtree 错误回调：去掉只读位后重试一次；再失败则放弃（由收尾检测报残留）。

    onerror（<3.12，第三参为 exc_info 元组）与 onexc（>=3.12，第三参为异常
    对象）均为三个位置参数，本回调忽略第三个参数的差异。
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        return


def remove_tree(path: Path | str) -> bool:
    """递归删除目录树（或单个文件），返回 True 表示目标已彻底不存在。

    只读文件先去写位重试；Windows 下自动加扩展长度前缀删除超长路径；
    删除结束后若目标仍存在，记 warning 并返回 False。
    目标本就不存在视为成功（幂等，允许并发重复调用）。
    """
    requested = Path(path)
    target = _extended_length_path(requested)
    if not target.exists() and not target.is_symlink():
        return True
    try:
        if target.is_dir() and not target.is_symlink():
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=_retry_with_write_bit)
            else:
                shutil.rmtree(target, onerror=_retry_with_write_bit)
        else:
            try:
                target.unlink()
            except PermissionError:
                os.chmod(target, stat.S_IWRITE)
                target.unlink()
    except Exception as exc:
        logger.warning(
            'fs_utils.remove_tree: 删除 {} 异常：{!r}（继续检测残留）',
            target,
            exc,
        )
    if target.exists() or target.is_symlink():
        try:
            leftovers = [str(item) for item in target.rglob('*')]
        except OSError:
            leftovers = []
        logger.warning(
            'fs_utils.remove_tree: {} 未能完全删除，残留 {} 项；前 {} 项：{}',
            target,
            len(leftovers),
            _MAX_REPORTED_LEFTOVERS,
            ', '.join(leftovers[:_MAX_REPORTED_LEFTOVERS]),
        )
        return False
    return True
