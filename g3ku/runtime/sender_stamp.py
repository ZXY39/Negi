"""模型可见投影里的入站来源标注。

渠道消息在转录里保持原文（RAG、网页回放、导出都吃 raw content），来源标注只在
构造模型请求时由投影层追加一行前缀，和 ``[消息送达时间]`` 是同一套纪律：

* 装饰值只派生自记录里的固定字段（``external_trust`` / ``external_sender`` /
  ``chat_type``），跨回合字节稳定，不打断 provider 前缀缓存；
* 所有基于内容相等性的比较必须先 :func:`strip_sender_stamp` 还原原文；
* 只给**第三方**消息盖标注。操作员自己的消息不盖——那是噪声，也会让存量 owner
  会话的稳定前缀无谓地变一次。

为什么这条标注值得进上下文：外部渠道的正文和操作员本人说的话在转录里长得一模一样，
模型分不出"群友让我干的事"和"我让我干的事"。工具面已经按信任级收紧了（控制面拒绝、
只读档），标注是给模型**解释**用的第二层——拒绝时它才说得出"这是第三方群聊"。
"""

from __future__ import annotations

from typing import Any

from g3ku.runtime.session_trust import (
    MULTI_PARTY_CHAT_TYPES,
    TRUST_OWNER,
    normalize_chat_type,
    normalize_trust,
)

MESSAGE_SENDER_MARKER = "[消息来源]"

_GROUP_LABEL = "群消息"
_DM_LABEL = "私聊"
_THIRD_PARTY_NOTE = "第三方，非操作员"


def _short_id(value: Any, *, limit: int = 8) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _one_line(value: Any) -> str:
    """发送者身份来自桥的请求体：折成一行，名字里的换行不能伪造出第二行标注。"""
    return " ".join(str(value or "").split())


def render_sender_stamp(metadata: Any) -> str:
    """按消息 metadata 渲染来源标注；不需要标注时返回空串。

    返回的是**带尾换行的一行前缀**，调用方直接 ``stamp + content``。
    """
    if not isinstance(metadata, dict):
        return ""
    trust = normalize_trust(metadata.get("external_trust"))
    if not trust or trust == TRUST_OWNER:
        return ""
    sender = metadata.get("external_sender")
    sender_id = ""
    sender_name = ""
    if isinstance(sender, dict):
        sender_id = _short_id(_one_line(sender.get("id")))
        sender_name = _one_line(sender.get("name"))
    chat_type = normalize_chat_type(metadata.get("chat_type"))
    kind = _GROUP_LABEL if chat_type in MULTI_PARTY_CHAT_TYPES else _DM_LABEL
    parts = [kind]
    if sender_name and sender_id:
        parts.append(f"发送者 {sender_name}({sender_id})")
    elif sender_id:
        parts.append(f"发送者 {sender_id}")
    elif sender_name:
        parts.append(f"发送者 {sender_name}")
    parts.append(_THIRD_PARTY_NOTE)
    return f"{MESSAGE_SENDER_MARKER} {' | '.join(parts)}\n"


def strip_sender_stamp(text: Any) -> str:
    """剥离投影层追加的来源标注行，还原用户原文。

    只认**首行以标记开头**这一种形状：用户正文里恰好出现标记文本不会被误删。
    """
    raw = str(text or "")
    if not raw.startswith(MESSAGE_SENDER_MARKER):
        return raw
    newline = raw.find("\n")
    if newline < 0:
        return ""
    return raw[newline + 1 :]


__all__ = ["MESSAGE_SENDER_MARKER", "render_sender_stamp", "strip_sender_stamp"]
