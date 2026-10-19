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


def placeholder_tool_schema(tool_name: str) -> dict[str, Any]:
    """参数表四档都取不到时的占位：名字留在 `tools[]`，参数表明示没带出来。

    钉住的是**成员**，不是编造契约。断点位置与"直接删名"完全一样（都从这一条的起始字节起
    断），所以占位不省缓存；它省的是语义——清单成员只在重印边界变，资源删除/重启这种
    边界之外的事件不许让一个名字悄悄离开 `tools[]`。空 `properties` 加这句描述，模型调它
    拿到的仍是执行准入那一层的真实回执，不会靠猜参数打出去。两条车道共用本函数，
    占位形状因此也是同一份字节。
    """
    name = _normalize(tool_name)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Parameter table is not carried into this turn. "
                "Do not guess its arguments; use another tool."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }
