"""契约在场判据（contract presence）。

一句话不变量：**一个工具在某一跳能不能被调用，取决于它的 toolskill 契约正文在这一跳
是不是在场。**

在场判据只有这一个函数，两个载体都喂给它：

1. 请求视图里**未被压缩删除**的 `role=tool` loader 结果行
   （`load_tool_context` / `load_tool_context_v2`，JSON 载荷 `ok:true` 且 `tool_id`
   与解析后的目标一致）；
2. 阶段台账里的 `kept_tool_contexts` 条目（同一 `tool_id` + 同一
   `tool_context_fingerprint`）。裁撤阶段肉身时把正文留在阶段块里，它同样算在场。

skill **不参与**本判据：skill 不进水合台账，没有 callable 可摘。

调用方只有三处（缺一不可，都在现存代码里）：节点 callable 组装
（`main/service/runtime_service.py`）、前门 callable 共享算点
（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py`，装配 / 缓存刷新 / 发送预检三个算点都
经它）、以及两条车道的重复读守卫。判据按**当次请求视图**——压缩跳成批摘能力、下一跳
成批还，属预期行为，不做防抖。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

LOADER_TOOL_NAMES = frozenset({"load_tool_context", "load_tool_context_v2"})

KEPT_TOOL_CONTEXTS_FIELD = "kept_tool_contexts"
TOOL_ID_FIELD = "tool_id"
FINGERPRINT_FIELD = "tool_context_fingerprint"
BODY_FIELD = "body"


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _message_field(message: Any, key: str) -> Any:
    """消息既可能是 dict 记录，也可能是 LangChain 对象；两种读法同价。"""
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def loader_payload(message: Any) -> dict[str, Any] | None:
    """把一条 loader 工具结果解成契约载荷；不是成功的 loader 结果时返回 ``None``。"""
    if str(_message_field(message, "role") or "").strip().lower() != "tool":
        return None
    if str(_message_field(message, "name") or "").strip() not in LOADER_TOOL_NAMES:
        return None
    content = _message_field(message, "content")
    if isinstance(content, dict):
        payload = content
    else:
        text = str(content or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        payload = parsed
    if not bool(payload.get("ok")):
        return None
    tool_id = str(payload.get(TOOL_ID_FIELD) or "").strip()
    fingerprint = str(payload.get(FINGERPRINT_FIELD) or "").strip()
    if not tool_id or not fingerprint:
        return None
    return payload


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


def _iter_kept_entries(kept_stage_contexts: Any) -> list[dict[str, Any]]:
    """接受三种形态：条目平表、阶段列表（每项带 ``kept_tool_contexts``）、单阶段字典。"""
    items = list(kept_stage_contexts or [])
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get(KEPT_TOOL_CONTEXTS_FIELD)
        if isinstance(nested, list):
            entries.extend([entry for entry in nested if isinstance(entry, dict)])
            continue
        if str(item.get(TOOL_ID_FIELD) or "").strip():
            entries.append(item)
    return entries


def kept_contract_index(kept_stage_contexts: Any) -> dict[str, str]:
    """阶段块里保留的契约：``tool_id -> fingerprint``，同名取后出现的条目。"""
    index: dict[str, str] = {}
    for entry in _iter_kept_entries(kept_stage_contexts):
        tool_id = str(entry.get(TOOL_ID_FIELD) or "").strip()
        if not tool_id:
            continue
        index[tool_id] = str(entry.get(FINGERPRINT_FIELD) or "").strip()
    return index


def contract_presence_index(
    *,
    request_messages: Iterable[Any] | None = None,
    kept_stage_contexts: Any = None,
) -> dict[str, str]:
    """两个载体合并后的在场索引；键集合就是在场的 ``tool_id``。"""
    index = loader_contract_index(request_messages)
    for tool_id, fingerprint in kept_contract_index(kept_stage_contexts).items():
        if tool_id not in index or not index.get(tool_id):
            index[tool_id] = fingerprint
    return index


def contract_presence(
    tool_id: str,
    *,
    request_messages: Iterable[Any] | None = None,
    kept_stage_contexts: Any = None,
) -> tuple[bool, str]:
    """契约正文在这一跳是否在场，以及当前生效的 fingerprint。

    返回 ``(present, tool_context_fingerprint)``；不在场时 fingerprint 为空串。
    """
    normalized = str(tool_id or "").strip()
    if not normalized:
        return False, ""
    index = contract_presence_index(
        request_messages=request_messages,
        kept_stage_contexts=kept_stage_contexts,
    )
    fingerprint = index.get(normalized)
    if fingerprint is None:
        return False, ""
    return True, str(fingerprint or "")


def partition_contract_presence(
    tool_ids: Iterable[Any] | None,
    *,
    request_messages: Iterable[Any] | None = None,
    kept_stage_contexts: Any = None,
    index: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """按在场与否把名字分成两组，各自保持入参顺序、去重。

    给批量调用点用（一次建索引、逐个查表），避免每个名字都重扫一遍历史。
    """
    effective_index = (
        index
        if index is not None
        else contract_presence_index(
            request_messages=request_messages,
            kept_stage_contexts=kept_stage_contexts,
        )
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


def normalize_kept_tool_contexts(raw: Any) -> list[dict[str, Any]]:
    """保留契约条目的归一化白名单：只留 ``tool_id`` / fingerprint / 正文。

    裁撤掉的阶段块里那段正文是**提交点的一次性快照**（刀二写入），逐轮只回放账本、
    不重读资源文件——否则运营者改一次 toolskill 就改动块字节，而块在中间位置，它之后
    的前缀全断。这里漏一个键等于逐轮抹掉一个键。
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in _iter_kept_entries(raw):
        tool_id = str(entry.get(TOOL_ID_FIELD) or "").strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        normalized.append(
            {
                TOOL_ID_FIELD: tool_id,
                FINGERPRINT_FIELD: str(entry.get(FINGERPRINT_FIELD) or "").strip(),
                BODY_FIELD: str(entry.get(BODY_FIELD) or ""),
            }
        )
    return normalized


def kept_tool_contexts_for_stages(stages: Iterable[Any] | None) -> list[dict[str, Any]]:
    """把阶段台账里各阶段的 ``kept_tool_contexts`` 摊平成一份在场来源。"""
    collected: list[dict[str, Any]] = []
    for stage in list(stages or []):
        if not isinstance(stage, dict):
            continue
        collected.extend(_iter_kept_entries(stage.get(KEPT_TOOL_CONTEXTS_FIELD)))
    return collected


def kept_tool_contexts_from_frames(payload: Any) -> list[dict[str, Any]]:
    """从阶段状态载荷（节点 ``execution_stages`` / 前门 ``frontdoor_stage_state``）取保留条目。"""
    if not isinstance(payload, dict):
        return []
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return kept_tool_contexts_for_stages(stages)


__all__ = [
    "LOADER_TOOL_NAMES",
    "KEPT_TOOL_CONTEXTS_FIELD",
    "contract_presence",
    "contract_presence_index",
    "kept_contract_index",
    "kept_tool_contexts_for_stages",
    "kept_tool_contexts_from_frames",
    "loader_contract_index",
    "loader_payload",
    "normalize_kept_tool_contexts",
    "partition_contract_presence",
]
