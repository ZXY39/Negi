"""契约标记块的唯一渲染与解析（在场载体的两端）。

一条不变量：**契约在场 = 模型可见上下文里存在这个标记块**，没有第二本账。因此渲染和
解析必须成对放在同一个模块里——两边各自实现一遍，字节对不上就是"看得见名字却调不动"
那类故障的源头。

块形如：

    [TOOL_CONTRACT_V1:filesystem_edit:tcf:01234546789abcdef]
    <契约正文>
    [/TOOL_CONTRACT_V1]

`load_tool_context(_v2)` 与未水合工具的冲动调用两条投递道都调 `render_tool_contract`，
产出逐字节相同的块（D14）。块放进结果载荷的 `content` 字段：信封形状（`ok` / `uri` /
`level` / `l0` / `l1`）是既有的外置与展示契约，不改它，只把正文换成带块的那一份，同一
条消息里不会出现两份正文。

执行失败或没有可交付契约的载荷**不含块**：判据据此判不在场。
"""

from __future__ import annotations

import json
import re
from typing import Any

MARKER_NAME = "TOOL_CONTRACT_V1"
MARKER_CLOSE = f"[/{MARKER_NAME}]"
CONTRACT_CONTENT_FIELD = "content"

# 块头 = `[TOOL_CONTRACT_V1:<tool_id>:<fingerprint>]`。tool_id 不含冒号/换行，
# fingerprint 是 `tcf:<hex>`；两个捕获组都不吃 `]`，所以正文里再出现块头也不会被误当成边界。
_BLOCK_HEADER_RE = re.compile(
    r"\[" + re.escape(MARKER_NAME) + r":([^\]\n:]+):([^\]\n]+)\]"
)


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def render_tool_contract_block(tool_id: str, fingerprint: str, body: str) -> str:
    """渲染一块契约。fingerprint 为空时返回空串——无身份的契约不构成在场，不许印半成品块。"""
    normalized_tool = _normalized_text(tool_id)
    normalized_fingerprint = _normalized_text(fingerprint)
    if not normalized_tool or not normalized_fingerprint:
        return ""
    text = str(body or "")
    return (
        f"[{MARKER_NAME}:{normalized_tool}:{normalized_fingerprint}]\n"
        f"{text.rstrip()}\n"
        f"{MARKER_CLOSE}"
    )


def contract_body_from_payload(payload: dict[str, Any] | None) -> str:
    """把 toolskill 载荷拼成块内正文；两条投递道共用，保证正文也逐字节相同。"""
    normalized = payload if isinstance(payload, dict) else {}
    sections: list[str] = []
    content = str(normalized.get(CONTRACT_CONTENT_FIELD) or "").strip()
    if content:
        sections.append(content)
    parameter_contract = str(normalized.get("parameter_contract_markdown") or "").strip()
    if parameter_contract:
        sections.append(parameter_contract)
    warnings = [_normalized_text(item) for item in list(normalized.get("warnings") or []) if _normalized_text(item)]
    if warnings:
        sections.append("Warnings:\n" + "\n".join(f"- {item}" for item in warnings))
    errors = [_normalized_text(item) for item in list(normalized.get("errors") or []) if _normalized_text(item)]
    if errors:
        sections.append("Not available:\n" + "\n".join(f"- {item}" for item in errors))
    return "\n\n".join(sections)


def render_tool_contract(payload: dict[str, Any] | None) -> str:
    """从载荷直接渲染块（载荷里已带 `tool_context_fingerprint`）。空块表示无可交付契约。"""
    normalized = payload if isinstance(payload, dict) else {}
    return render_tool_contract_block(
        _normalized_text(normalized.get("tool_id")),
        _normalized_text(normalized.get("tool_context_fingerprint")),
        contract_body_from_payload(normalized),
    )


def attach_tool_contract_block(payload: dict[str, Any] | None) -> dict[str, Any]:
    """把块写进 `content` 并原样返回载荷；无可交付契约时返回原载荷（不印空块）。

    `l0` / `l1` 是既有的预览层，按纯正文算，不跟着带标记——它们服务窗口化，不服务在场判据。
    """
    normalized = payload if isinstance(payload, dict) else {}
    block = render_tool_contract(normalized)
    if not block:
        return normalized
    next_payload = dict(normalized)
    next_payload[CONTRACT_CONTENT_FIELD] = block
    return next_payload


def parse_tool_contract_blocks(text: Any) -> list[tuple[str, str]]:
    """按出现顺序取出 `(tool_id, fingerprint)`；正文里的同名块头不参与边界判定。"""
    content = str(text or "")
    if MARKER_NAME not in content:
        return []
    pairs: list[tuple[str, str]] = []
    for match in _BLOCK_HEADER_RE.finditer(content):
        tool_id = _normalized_text(match.group(1))
        fingerprint = _normalized_text(match.group(2))
        if tool_id and fingerprint:
            pairs.append((tool_id, fingerprint))
    return pairs


def contract_text_segments(value: Any) -> list[str]:
    """一条结果里值得扫块的位置：整串文本，以及 JSON 信封的 `content` 字段。

    两条投递道都经 `json.dumps`，块在模型看到的是转义形态；判据要先拿到未转义的那份文本
    才能比 fingerprint，所以信封形态必须单独取字段，不能只扫整串。
    """
    if isinstance(value, dict):
        segments: list[str] = []
        content = value.get(CONTRACT_CONTENT_FIELD)
        if isinstance(content, str):
            segments.append(content)
        segments.append(json.dumps(value, ensure_ascii=False, default=str))
        return segments
    text = str(value or "")
    if not text:
        return []
    segments = [text]
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            content = parsed.get(CONTRACT_CONTENT_FIELD)
            if isinstance(content, str):
                segments.append(content)
    return segments


def contract_pairs_from_value(value: Any) -> list[tuple[str, str]]:
    """一条结果里的在场对子。闭标签必须存在——半截块（外置信封截断）不算在场。"""
    for segment in contract_text_segments(value):
        if MARKER_CLOSE not in segment:
            continue
        pairs = parse_tool_contract_blocks(segment)
        if pairs:
            return pairs
    return []


def contract_text_length(value: Any) -> int:
    """契约文本的字符数，给外置闸门判上限用（不含信封元数据）。"""
    best = 0
    for segment in contract_text_segments(value):
        if parse_tool_contract_blocks(segment):
            best = max(best, len(segment))
    return best
