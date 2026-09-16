from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def extract_call_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "|" in raw:
        return raw.split("|", 1)[0].strip()
    return raw


@dataclass(slots=True)
class ToolCallHistoryAnalysis:
    declared_call_ids: list[str] = field(default_factory=list)
    completed_call_ids: list[str] = field(default_factory=list)
    orphan_tool_result_ids: list[str] = field(default_factory=list)
    dangling_assistant_call_ids: list[str] = field(default_factory=list)

    @property
    def has_orphan_tool_results(self) -> bool:
        return bool(self.orphan_tool_result_ids)

    @property
    def has_dangling_assistant_calls(self) -> bool:
        return bool(self.dangling_assistant_call_ids)


def analyze_tool_call_history(messages: list[dict[str, Any]]) -> ToolCallHistoryAnalysis:
    declared_seen: set[str] = set()
    completed_seen: set[str] = set()
    declared_call_ids: list[str] = []
    completed_call_ids: list[str] = []

    for msg in list(messages or []):
        role = str((msg or {}).get("role") or "").strip().lower()
        if role == "assistant":
            for tool_call in list((msg or {}).get("tool_calls") or []):
                call_id = extract_call_id((tool_call or {}).get("id"))
                if call_id and call_id not in declared_seen:
                    declared_seen.add(call_id)
                    declared_call_ids.append(call_id)
            continue
        if role == "tool":
            call_id = extract_call_id((msg or {}).get("tool_call_id"))
            if call_id and call_id not in completed_seen:
                completed_seen.add(call_id)
                completed_call_ids.append(call_id)

    declared_set = set(declared_call_ids)
    completed_set = set(completed_call_ids)
    orphan_tool_result_ids = [call_id for call_id in completed_call_ids if call_id not in declared_set]
    dangling_assistant_call_ids = [call_id for call_id in declared_call_ids if call_id not in completed_set]

    return ToolCallHistoryAnalysis(
        declared_call_ids=declared_call_ids,
        completed_call_ids=completed_call_ids,
        orphan_tool_result_ids=orphan_tool_result_ids,
        dangling_assistant_call_ids=dangling_assistant_call_ids,
    )


def align_compaction_keep_recent(messages: list[dict[str, Any]] | None, keep_recent: int) -> int:
    """把压缩保留尾部的边界对齐到完整的工具调用组。

    纯条数的尾部切片（``messages[-keep_recent:]``）可能把边界落在工具调用组中间：
    尾部首条是 ``role=tool`` 结果、而声明它的 ``assistant(tool_calls)`` 消息落在更早的
    可压缩区时，重写后的请求会包含孤儿工具结果（节点侧触发 3-strike 熔断，会话侧
    静默污染请求；事故：task:25745b5268dc）。尾部是列表后缀，声明之后的结果必然
    同在尾部，因此只需保证尾部首条不是 tool 结果：向前扩展边界直到首条非 tool 消息。

    扩展上界为单个工具批次的消息数；最坏情况整个 body 成为尾部、无可压缩历史，
    由调用方按既有的"无可压缩历史"分支处理。
    """
    seq = [item for item in list(messages or []) if isinstance(item, dict)]
    total = len(seq)
    k = max(0, int(keep_recent or 0))
    if k <= 0:
        return 0
    if k >= total:
        return total
    while k < total:
        candidate = seq[-k]
        role = str((candidate or {}).get("role") or "").strip().lower()
        if role != "tool":
            break
        k += 1
    return k


def iter_compaction_atomic_groups(messages: list[dict[str, Any]] | None) -> list[list[dict[str, Any]]]:
    """把消息序列切成压缩不可再分的原子组，供分块压缩装箱。

    原子组 = 单条非工具消息，或一个完整工具调用组（``assistant(tool_calls)`` +
    紧随其后的全部 ``role=tool`` 结果）。分块边界落在原子组之间，保证任何一块
    都不会切断工具调用组（与 ``align_compaction_keep_recent`` 同一不变量）。
    孤儿工具结果（无前置声明）自成一組，不向前合并。
    """
    seq = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
    groups: list[list[dict[str, Any]]] = []
    index = 0
    total = len(seq)
    while index < total:
        msg = seq[index]
        role = str((msg or {}).get("role") or "").strip().lower()
        if role == "assistant" and list((msg or {}).get("tool_calls") or []):
            group = [msg]
            index += 1
            while index < total:
                nxt = seq[index]
                if str((nxt or {}).get("role") or "").strip().lower() != "tool":
                    break
                group.append(nxt)
                index += 1
            groups.append(group)
            continue
        groups.append([msg])
        index += 1
    return groups
