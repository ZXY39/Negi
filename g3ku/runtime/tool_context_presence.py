"""契约在场判据（contract presence）。

一句话不变量：**一个工具在某一跳能不能被调用，取决于它的 toolskill 契约标记块在这一跳
是不是在模型可见上下文里。**

在场判据只有这一个函数，且只有**一个载体**：请求视图里未被压缩删除的消息文本里的
`[TOOL_CONTRACT_V1:<tool_id>:<fingerprint>]` 块（块与解析成对见
`g3ku/runtime/tool_context_block.py`）。两条投递道（`load_tool_context(_v2)` 与未水合
工具的冲动调用）都印同一个块，所以判据不需要知道是谁送的。

裁撤即离场，下次调用走冲动路径重新拿契约（无感自愈）。

skill **不参与**本判据：skill 没有 callable 可摘。

调用方只有三处（缺一不可）：节点 callable 组装（`main/service/runtime_service.py`）、
前门 callable 共享算点（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py`，装配 / 缓存刷新 /
发送预检三个算点都经它）、以及两条车道的重复读守卫。判据按**当次请求视图**——压缩跳成批
摘能力、下一跳成批还，属预期行为，不做防抖。
"""

from __future__ import annotations

from typing import Any, Iterable

from g3ku.runtime.tool_contract_block import contract_pairs_from_value

LOADER_TOOL_NAMES = frozenset({"load_tool_context", "load_tool_context_v2"})

TOOL_ID_FIELD = "tool_id"
FINGERPRINT_FIELD = "tool_context_fingerprint"
BODY_FIELD = "body"


def _message_field(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def loader_payload(message: Any) -> dict[str, Any] | None:
    """把一条消息解成契约载荷；块不在场时返回 ``None``。

    不再按 `name ∈ LOADER_TOOL_NAMES` 认领：交付者是谁与契约在不在场是两件事，未水合工具
    的冲动调用结果同样带块。返回的载荷只保证 ``tool_id`` 与 ``tool_context_fingerprint``
    两项（来自块头）。
    """
    pairs = contract_pairs_from_value(_message_field(message, "content"))
    if not pairs:
        return None
    tool_id, fingerprint = pairs[-1]
    return {TOOL_ID_FIELD: tool_id, FINGERPRINT_FIELD: fingerprint}


def loader_contract_index(request_messages: Iterable[Any] | None) -> dict[str, str]:
    """请求视图里在场的契约：``tool_id -> fingerprint``，同一目标取最近一条。"""
    index: dict[str, str] = {}
    for message in reversed(list(request_messages or [])):
        payload = loader_payload(message)
        if payload is None:
            continue
        tool_id = str(payload.get(TOOL_ID_FIELD) or "").strip()
        if not tool_id or tool_id in index:
            continue
        index[tool_id] = str(payload.get(FINGERPRINT_FIELD) or "").strip()
    return index


def contract_presence_index(
    *,
    request_messages: Iterable[Any] | None = None,
) -> dict[str, str]:
    """在场索引；键集合就是在场的 ``tool_id``。

    `kept_stage_contexts` 参数已摘：阶段账本不再算第二载体（D9），留着一个"传了也不认"
    的参数位，等于给下一个人递一把造漂移的枪。
    """
    return loader_contract_index(request_messages)


def contract_presence(
    tool_id: str,
    *,
    request_messages: Iterable[Any] | None = None,
) -> tuple[bool, str]:
    """契约标记块在这一跳是否在场，以及当前生效的 fingerprint。

    返回 ``(present, tool_context_fingerprint)``；不在场时 fingerprint 为空串。
    """
    normalized = str(tool_id or "").strip()
    if not normalized:
        return False, ""
    fingerprint = contract_presence_index(request_messages=request_messages).get(normalized)
    if fingerprint is None:
        return False, ""
    return True, str(fingerprint or "")


def partition_contract_presence(
    tool_ids: Iterable[Any] | None,
    *,
    request_messages: Iterable[Any] | None = None,
    index: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """按在场与否把名字分成两组，各自保持入参顺序、去重。

    给批量调用点用（一次建索引、逐个查表），避免每个名字都重扫一遍历史。
    """
    effective_index = (
        index
        if index is not None
        else contract_presence_index(request_messages=request_messages)
    )
    present: list[str] = []
    absent: list[str] = []
    seen: set[str] = set()
    for raw in list(tool_ids or []):
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name in effective_index:
            present.append(name)
        else:
            absent.append(name)
    return present, absent


def stale_rev_tool_ids(
    tool_ids: Iterable[Any] | None,
    *,
    request_messages: Iterable[Any] | None = None,
    current_fingerprint_of: Any = None,
) -> list[str]:
    """块在、但修订不是当前修订的名字（D15 的 `stale_rev` 档）。

    在场索引只回答"有没有块"，修订是否过期要拿当前载荷的指纹比一次——这一档是 D2 换式
    与 D10 外置吞块两个坑的唯一可见信号。
    """
    index = contract_presence_index(request_messages=request_messages)
    stale: list[str] = []
    for raw in list(tool_ids or []):
        name = str(raw or "").strip()
        if not name or name not in index:
            continue
        if not callable(current_fingerprint_of):
            continue
        current = str(current_fingerprint_of(name) or "").strip()
        if current and current != str(index.get(name) or ""):
            stale.append(name)
    return stale


__all__ = [
    "BODY_FIELD",
    "FINGERPRINT_FIELD",
    "LOADER_TOOL_NAMES",
    "TOOL_ID_FIELD",
    "contract_presence",
    "contract_presence_index",
    "loader_contract_index",
    "loader_payload",
    "partition_contract_presence",
    "stale_rev_tool_ids",
]
