"""路径根限定：只读档的执行边界。

工具层的**唯一**根判定实现。两个来源：

* ``runtime_context["fs_read_roots"]`` / ``["fs_write_roots"]``：按会话给出的
  档位根（只读档由前门写进 runtime context）。**它比工具设置更窄，优先级更高**——
  只读档不能被 ``restrict_to_workspace`` 这类宽松设置反向放宽。
* 工具设置（``restrict_to_workspace``）：旧行为，根就是工作区。

判定一律在 ``Path.resolve()`` **之后**做：symlink 指到根外会被拒。
空的根清单 = 不设限（宿主全权限档），**不是**"全部拒绝"——这一点与 fail-closed
的取舍相反，所以调用方只在"确实没有策略"时才会拿到空清单。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable, Iterator

RootInput = Path | str | Iterable[Path | str] | None

# 当次工具调用的档位根。由执行闸门（前门 `_execute_tool_call_with_raw_result` /
# 节点 `_execute_tool_raw`）在调用前后 pin/重置：工具实例是按资源缓存的共享对象，
# 不能把会话根写到实例上；ContextVar 又天然按 asyncio 任务隔离，不会串会话。
_CURRENT_READ_ROOTS: ContextVar[tuple[Path, ...]] = ContextVar(
    "g3ku_fs_read_roots", default=()
)


@contextmanager
def pinned_read_roots(roots: RootInput) -> Iterator[None]:
    token = _CURRENT_READ_ROOTS.set(normalize_roots(roots))
    try:
        yield
    finally:
        _CURRENT_READ_ROOTS.reset(token)


def current_read_roots() -> tuple[Path, ...]:
    return _CURRENT_READ_ROOTS.get()


def normalize_roots(value: RootInput) -> tuple[Path, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        candidates: list[Any] = [value]
    else:
        try:
            candidates = list(value)
        except TypeError:
            return ()
    roots: list[Path] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            resolved = Path(text).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def is_within_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def enforce_roots(path: Path, roots: tuple[Path, ...], *, label: str = "Path") -> Path:
    """根外一律 ``PermissionError``；空根清单表示不设限。"""
    if not roots:
        return path
    if is_within_roots(path, roots):
        return path
    raise PermissionError(f"{label} is outside the allowed roots: {path}")


def runtime_roots(runtime: dict[str, Any] | None, key: str) -> tuple[Path, ...]:
    if not isinstance(runtime, dict):
        return ()
    return normalize_roots(runtime.get(key))


def runtime_read_roots(runtime: dict[str, Any] | None) -> tuple[Path, ...]:
    roots = runtime_roots(runtime, "fs_read_roots")
    return roots or current_read_roots()


def runtime_write_roots(runtime: dict[str, Any] | None) -> tuple[Path, ...]:
    return runtime_roots(runtime, "fs_write_roots")


def resolve_allowed_roots(
    *,
    runtime: dict[str, Any] | None,
    key: str,
    fallback_roots: RootInput = None,
) -> tuple[Path, ...]:
    """运行时根优先；没有运行时根才用设置推出的根。"""
    override = runtime_roots(runtime, key)
    if override:
        return override
    return normalize_roots(fallback_roots)


__all__ = [
    "RootInput",
    "current_read_roots",
    "enforce_roots",
    "is_within_roots",
    "normalize_roots",
    "pinned_read_roots",
    "resolve_allowed_roots",
    "runtime_read_roots",
    "runtime_roots",
    "runtime_write_roots",
]
