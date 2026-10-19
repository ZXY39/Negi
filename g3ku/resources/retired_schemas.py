"""被删除工具的 provider 参数表快照。

删除资源会把实例从资源快照里摘掉，而 `tools[]` 是按活实例现渲的——没有快照，下一跳
工具表就少一条，可复用前缀从那里断开。权限收回不会断，因为实例还在、参数表由声明侧
继续供着；删除要享受同一条缓存待遇，就必须在实例消失前把它的 schema 抄一份下来。

调用方只有两处：删除流程在 rmtree 之前 `record_tool_schema(...)`；渲染侧在名字没有
活实例时 `lookup_tool_schema(...)` 取回同一份字节。条目有界（LRU），进程重启即清空——
重启本身就是清单重新播种的边界，不需要持久化。
"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from typing import Any

_MAX_ENTRIES = 128

_lock = threading.Lock()
_entries: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _normalize(name: Any) -> str:
    return str(name or "").strip()


def record_tool_schema(tool_name: str, schema: Any) -> None:
    """记下某个工具删除前的 provider schema。非 dict 一律忽略。"""
    name = _normalize(tool_name)
    if not name or not isinstance(schema, dict) or not schema:
        return
    with _lock:
        _entries[name] = copy.deepcopy(schema)
        _entries.move_to_end(name)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)


def lookup_tool_schema(tool_name: str) -> dict[str, Any] | None:
    """取回删除前记下的 schema；没有就返回 None。返回副本，调用方可随意改。"""
    name = _normalize(tool_name)
    if not name:
        return None
    with _lock:
        schema = _entries.get(name)
        if schema is None:
            return None
        _entries.move_to_end(name)
        return copy.deepcopy(schema)


def forget_tool_schema(tool_name: str) -> None:
    """同名工具重新装回来时丢掉旧快照：活实例永远优先，留着只会占位。"""
    name = _normalize(tool_name)
    if not name:
        return
    with _lock:
        _entries.pop(name, None)


def clear_tool_schemas() -> None:
    """测试与运维重置用：清空全部快照。"""
    with _lock:
        _entries.clear()


def schema_name(schema: Any) -> str:
    """取一份 provider schema 的工具名，wrapped 与扁平两种形状同源。

    落盘的 `actual_tool_schemas` 与活实例渲出来的形状可能不同（`{"type":"function",
    "function":{...}}` 与 `{"type":"function","name":...,"parameters":...}` 是同一逻辑
    契约的两种表示），按名字索引必须两边都认，否则上一跳原文里的名字会查不出来。
    """
    if not isinstance(schema, dict):
        return ""
    inner = schema.get("function")
    if isinstance(inner, dict):
        return _normalize(inner.get("name"))
    return _normalize(schema.get("name"))


def index_schemas_by_name(schemas: Any) -> dict[str, dict[str, Any]]:
    """把一份 schema 列表按工具名索引；重名保留第一份，非 dict 与无名条目丢弃。"""
    index: dict[str, dict[str, Any]] = {}
    for schema in list(schemas or []):
        if not isinstance(schema, dict):
            continue
        name = schema_name(schema)
        if name and name not in index:
            index[name] = schema
    return index
