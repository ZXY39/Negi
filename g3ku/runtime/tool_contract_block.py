"""契约标记块的唯一渲染与解析（在场载体的两端）。

一条不变量：**契约在场 = 模型可见上下文里存在这个标记块**，没有第二本账。因此渲染和
解析必须成对放在同一个模块里——两边各自实现一遍，字节对不上就是"看得见名字却调不动"
那类故障的源头。

块形如：

    [TOOL_CONTRACT:filesystem_edit:tcf:01234546789abcdef]
    <契约正文>
    [/TOOL_CONTRACT]

存量请求体里的旧拼写 `[TOOL_CONTRACT_V1:…]` / `[/TOOL_CONTRACT_V1]` 读侧同认，写侧只出新拼写。

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

MARKER_NAME = "TOOL_CONTRACT"
# 存量请求体、基线、侧车里带着带版本号的旧拼写：只读不写。
MARKER_NAME_ALIASES = ("TOOL_CONTRACT_V1",)
MARKER_NAMES = (MARKER_NAME, *MARKER_NAME_ALIASES)
MARKER_CLOSE = f"[/{MARKER_NAME}]"
MARKER_CLOSES = tuple(f"[/{name}]" for name in MARKER_NAMES)
CONTRACT_CONTENT_FIELD = "content"

# 块头 = `[TOOL_CONTRACT:<tool_id>:<fingerprint>]`（以及旧拼写）。tool_id 不含冒号/换行，
# fingerprint 是 `tcf:<hex>`；两个捕获组都不吃 `]`，所以正文里再出现块头也不会被误当成边界。
_BLOCK_HEADER_RE = re.compile(
    r"\[(?:" + "|".join(re.escape(name) for name in MARKER_NAMES) + r"):([^\]\n:]+):([^\]\n]+)\]"
)


def _contains_marker(text: str) -> bool:
    return any(name in text for name in MARKER_NAMES)


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


def _leading_block_pairs(text: str) -> list[tuple[str, str]]:
    """只认**开头**那一块：`^[TOOL_CONTRACT:<tool>:<fp>] … [/TOOL_CONTRACT]`（旧拼写同认）。

    锚在开头而不是全文扫，是因为块里的正文本身可能出现一颗长得一样的块头（别的工具的
    toolskill 里抄了一段示例）。全文扫会把那颗假头当成第二份契约认下来，等于让一份契约
    正文替另一个工具声明"我的契约在场了"——在场只是三道闸门里的一道，但这条不变量
    （执行发生在契约对模型可见之后）正是本次改造的全部依据，不能让正文自己造出来。
    """
    if not text or not any(close in text for close in MARKER_CLOSES):
        return []
    stripped = text.lstrip()
    match = _BLOCK_HEADER_RE.match(stripped)
    if match is None:
        return []
    if not any(close in stripped for close in MARKER_CLOSES):
        return []
    tool_id = _normalized_text(match.group(1))
    fingerprint = _normalized_text(match.group(2))
    if not tool_id or not fingerprint:
        return []
    return [(tool_id, fingerprint)]


def _candidate_contract_texts(value: Any) -> list[str]:
    """一条结果里值得判块的位置。先做子串预筛，再决定要不要花一次 JSON 解析。

    在场判据每跳对**每一条**工具行都跑，历史里绝大多数行不是契约。块头本身不含任何会被
    转义的字符，所以 `_contains_marker(text)` 这一步在整串上就等价于在字段上，用它把
    `json.loads` 挡在真正的契约行之前——去掉 loader 名字闸门换来的那点开销就在这里补回。
    """
    if isinstance(value, dict):
        content = value.get(CONTRACT_CONTENT_FIELD)
        return [content] if isinstance(content, str) and _contains_marker(content) else []
    text = str(value or "")
    if not text or not _contains_marker(text):
        return []
    candidates = [text]
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            content = parsed.get(CONTRACT_CONTENT_FIELD)
            if isinstance(content, str):
                candidates.insert(0, content)
    return candidates


def contract_pairs_from_value(value: Any) -> list[tuple[str, str]]:
    """一条结果里的在场对子。半截块（外置信封截断、缺闭标签）不算在场。"""
    for segment in _candidate_contract_texts(value):
        pairs = _leading_block_pairs(segment)
        if pairs:
            return pairs
    return []


def contract_text_length(value: Any) -> int:
    """契约文本的字符数，给外置闸门判上限用（不含信封元数据）。"""
    candidates = _candidate_contract_texts(value)
    for segment in candidates:
        if _leading_block_pairs(segment):
            return len(segment)
    for segment in candidates:
        if _contains_marker(segment):
            return len(segment)
    return 0
