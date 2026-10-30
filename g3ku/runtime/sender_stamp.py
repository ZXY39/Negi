"""模型可见投影里的入站来源标注。

渠道消息在转录里保持原文（RAG、网页回放、导出都吃 raw content），来源标注只在
构造模型请求时由投影层追加一行前缀，和 ``[消息送达时间]`` 是同一套纪律：

* 装饰值只派生自记录里的固定字段（``external_trust`` / ``external_sender`` /
  ``chat_type`` / ``external_source``），跨回合字节稳定，不打断 provider 前缀缓存；
* 所有基于内容相等性的比较必须先 :func:`strip_sender_stamp` 还原原文；
* 没有渠道标记（既无 ``external_trust`` 也非网页输入框）的消息不盖标注——web 普通
  会话因此永远拿不到这一行。

身份形态：本人标 ``Master``；其余发送者按**会话内首次出现顺序**编号
（``0002``、``0003``…，``0001`` 留给 Master 但不印号），同一会话内同一 id 恒定
同一号。哈希不进模型上下文。

为什么这条标注值得进上下文：外部渠道的正文和操作员本人说的话在转录里长得一模一样，
模型分不出"群友让我干的事"和"我让我干的事"。工具面已经按信任级收紧了（控制面拒绝、
只读档），标注是给模型**解释**用的第二层——拒绝时它才说得出"这是第三方群聊"。
"""

from __future__ import annotations

from typing import Any, Iterable

from g3ku.runtime.session_trust import (
    MULTI_PARTY_CHAT_TYPES,
    TRUST_OWNER,
    is_trusted_sender,
    normalize_chat_type,
    normalize_trust,
)

MESSAGE_SENDER_MARKER = "[来源]"
WEB_COMPOSER_SOURCE = "web_composer"

_GROUP_LABEL = "群聊"
_DM_LABEL = "私聊"
_COMPOSER_LABEL = "输入框"
_MASTER_LABEL = "Master"
_THIRD_PARTY_LABEL = "第三方"
_UNKNOWN_LABEL = "未知"


def _one_line(value: Any) -> str:
    """发送者身份来自桥的请求体：折成一行，名字里的换行不能伪造出第二行标注。"""
    return " ".join(str(value or "").split())


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "").strip().lower()
    return str(getattr(message, "role", "") or "").strip().lower()


def _message_metadata(message: Any) -> dict[str, Any]:
    metadata = message.get("metadata") if isinstance(message, dict) else getattr(message, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def sender_identity(metadata: Any) -> tuple[str, str]:
    """``(id, name)``；桥没带 sender 时返回两个空串。"""
    if not isinstance(metadata, dict):
        return "", ""
    sender = metadata.get("external_sender")
    if not isinstance(sender, dict):
        return "", ""
    return _one_line(sender.get("id")), _one_line(sender.get("name"))


def sender_number_map(
    messages: Iterable[Any],
    *,
    master_ids: Iterable[Any] = (),
) -> dict[str, int]:
    """第三方发送者的会话内编号：按首次出现顺序从 ``2`` 起。

    输入是该会话的转录行（dict 或带 ``role``/``metadata`` 的对象）。每次投影重算，
    不落盘、不改转录：新会话按新格式投影，历史行在同一份转录上得到同一个号。
    本人不占号：``external_trust == owner`` 的行，以及 sender id 命中名单的行
    （群里本人的档位仍是第三方，但标签是 ``Master``，不该把 0002 吃掉）。
    """
    numbers: dict[str, int] = {}
    next_number = 2
    owners = frozenset(str(item or "").strip().lower() for item in master_ids or () if str(item or "").strip())
    for message in messages or ():
        if _message_role(message) != "user":
            continue
        metadata = _message_metadata(message)
        trust = normalize_trust(metadata.get("external_trust"))
        if not trust or trust == TRUST_OWNER:
            continue
        sender_id, _name = sender_identity(metadata)
        if not sender_id or sender_id in numbers:
            continue
        if owners and sender_id.lower() in owners:
            continue
        numbers[sender_id] = next_number
        next_number += 1
    return numbers


def resolve_sender_number(sender_id: Any, numbers: dict[str, int] | None) -> int | None:
    """已出现的发送者取映射里的号；尚未进历史的取现有最大号 + 1（落盘后重算同号）。"""
    key = str(sender_id or "").strip()
    if not key:
        return None
    mapping = numbers if isinstance(numbers, dict) else {}
    if key in mapping:
        return int(mapping[key])
    return max(mapping.values(), default=1) + 1


def sender_display_label(
    metadata: Any,
    *,
    number: int | None = None,
    master_ids: Iterable[Any] = (),
) -> str:
    """气泡与模型行共用的身份尾串（不含 ``[来源]`` 与群聊/私聊前缀）。

    返回空串 = 这条消息不该有标注（web 普通会话、助手行、内部行）。
    """
    if not isinstance(metadata, dict):
        return ""
    if str(metadata.get("external_source") or "").strip().lower() == WEB_COMPOSER_SOURCE:
        return _COMPOSER_LABEL
    trust = normalize_trust(metadata.get("external_trust"))
    if not trust:
        return ""
    sender_id, sender_name = sender_identity(metadata)
    if trust == TRUST_OWNER or is_trusted_sender(sender_id, owner_openids=master_ids):
        return _MASTER_LABEL
    if not sender_id and not sender_name:
        return _UNKNOWN_LABEL
    label = f"{_THIRD_PARTY_LABEL} {int(number):04d}" if number is not None else _THIRD_PARTY_LABEL
    return f"{label}（{sender_name}）" if sender_name else label


def render_sender_stamp(
    metadata: Any,
    *,
    number: int | None = None,
    master_ids: Iterable[Any] = (),
) -> str:
    """按消息 metadata 渲染来源标注；不需要标注时返回空串。

    返回的是**带尾换行的一行前缀**，调用方直接 ``stamp + content``。
    """
    label = sender_display_label(metadata, number=number, master_ids=master_ids)
    if not label:
        return ""
    if label == _COMPOSER_LABEL:
        # 气泡上只显示「输入框」；模型行补上身份，读起来是「输入框 Master」。
        return f"{MESSAGE_SENDER_MARKER} {label} {_MASTER_LABEL}\n"
    chat_type = normalize_chat_type((metadata or {}).get("chat_type"))
    kind = _GROUP_LABEL if chat_type in MULTI_PARTY_CHAT_TYPES else _DM_LABEL
    return f"{MESSAGE_SENDER_MARKER} {kind} {label}\n"


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


__all__ = [
    "MESSAGE_SENDER_MARKER",
    "WEB_COMPOSER_SOURCE",
    "render_sender_stamp",
    "resolve_sender_number",
    "sender_display_label",
    "sender_identity",
    "sender_number_map",
    "strip_sender_stamp",
]
