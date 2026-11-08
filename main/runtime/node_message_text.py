"""Rendering of node-mailbox messages as model-visible text.

The mailbox row keeps the raw body; the relation label and source id are
rendered at injection time. Keeping the prefix out of storage means:

* the sender's content stays byte-stable for fingerprints and history;
* a replay/archive can re-render the same header from the row;
* the notice tail window can compare bodies after stripping the one-line
  header instead of comparing a prefixed text with a raw one.
"""

from __future__ import annotations

import re
from typing import Any

from main.runtime.node_message_kind import (
    KIND_ACCEPTANCE_VERDICT,
    KIND_CHATBOX_DIRECT,
    KIND_CHILD_ABNORMAL_PAUSE,
    KIND_CHILD_FINAL,
    KIND_CHILD_PROGRESS,
    KIND_FRONTDOOR_NOTICE,
    KIND_PARENT_NOTICE,
    KIND_PEER_NOTICE,
    KIND_USER_DIRECT,
    KIND_WAKE_TIMER,
)

_LABEL_BY_KIND = {
    KIND_PARENT_NOTICE: '来自父节点',
    KIND_CHILD_PROGRESS: '来自子节点',
    KIND_CHILD_FINAL: '来自子节点',
    KIND_CHILD_ABNORMAL_PAUSE: '来自子节点',
    KIND_PEER_NOTICE: '来自同级节点',
    KIND_ACCEPTANCE_VERDICT: '来自验收节点',
    KIND_FRONTDOOR_NOTICE: '来自会话',
    KIND_USER_DIRECT: '来自用户',
    KIND_CHATBOX_DIRECT: '来自输入框',
    # 唤醒通知走「来自系统」这一档，不是漏填：`_PREFIX_RE` 只认这个家族里的标签，
    # 而 notice tail 的去重与被验节点的正文比对都靠 `strip_node_message_prefix` 剥这一行。
    # 给新 kind 起一个新前缀（例如「来自定时器」）会让剥前缀那条路认不出这行，
    # 于是同一份唤醒在尾块里既算"原文"又算"未消费通知"。
    KIND_WAKE_TIMER: '来自系统',
}

_PREFIX_RE = re.compile(
    r'^来自(?:父节点|子节点|同级节点|验收节点|会话|用户|输入框|系统)'
    r'(?:，ID是[^，\n]+)?，内容为：$'
)


def message_label(kind: str, *, system_relay: bool = False) -> str:
    if system_relay:
        return '来自系统'
    return _LABEL_BY_KIND.get(str(kind or '').strip(), '来自系统')


# 前门直接问哪个节点，就该由那个节点自己答，不必把话经父节点转到根节点再出口。
# 这句只挂在带 `payload.expects_reply` 的通知行上（运行时投递时写，模型传不了），
# 由渲染层每跳补：台账正文与 fingerprint 都不动，删掉这里等于摘掉这句。
FRONTDOOR_REPLY_TAIL = (
    'This message came from the front door and expects an answer on the message lane: '
    '`send_message(to="frontdoor", content=...)`. Text you write in your own turn does not '
    'reach the front door.'
)

# 同一机制的第二条车道：chatbox（节点详情页输入框）来的消息要求回投到操作员
# 专属线程，而不是 frontdoor 会话。与 FRONTDOOR_REPLY_TAIL 一样按行上的 payload
# 标记每跳重算，台账正文与 fingerprint 一字不动。
CHATBOX_REPLY_TAIL = (
    "This message came from the Master's chat box on your node detail page and expects an "
    'answer in that same thread: `send_message(to="chatbox", content=...)`. Text you write '
    "in your own turn does not reach the Master's chat box."
)

# 验收镜像行的定位说明：正文是被验执行节点收到的原信，镜像行挂在验收节点信箱里，
# 验收方注入时若不加说明会把"发给被验节点的要求"误读成"发给验收方的新指令"。
# 与前两条尾句同机制：按行上的 `payload.mirrored_from` 每跳重算，台账正文与
# fingerprint 一字不动。
ACCEPTANCE_MIRROR_TAIL = (
    'This message was addressed to the inspected execution node while it waits for your '
    'verdict; the runtime mirrored it into your mailbox so you can judge with it in view. '
    'It is not a new instruction to you: if it changes the requirements, reject the current '
    'submission and tell the node to continue per this message; if it does not, keep verifying.'
)


def notification_mirrored_from(record: Any) -> str:
    """镜像行的来源标记：返回被验节点信箱里的原行 id，非镜像行返回空串。"""
    payload = record.get('payload') if isinstance(record, dict) else getattr(record, 'payload', None)
    if not isinstance(payload, dict):
        return ''
    return str(payload.get('mirrored_from') or '').strip()


def notification_expects_reply(record: Any) -> bool:
    """通知行是否带「要求回投」标记：只读 payload，正文一个字都不参与判定。"""
    payload = record.get('payload') if isinstance(record, dict) else getattr(record, 'payload', None)
    return bool(payload.get('expects_reply')) if isinstance(payload, dict) else False


def notification_reply_via(record: Any) -> str:
    """通知行的回投车道标记：``chatbox`` 表示回投操作员线程，其余值不挂尾句。"""
    payload = record.get('payload') if isinstance(record, dict) else getattr(record, 'payload', None)
    if not isinstance(payload, dict):
        return ''
    value = str(payload.get('reply_via') or '').strip().lower()
    return value if value == 'chatbox' else ''


def render_node_message_text(
    *,
    kind: str,
    source_node_id: str = '',
    message: str,
    system_relay: bool = False,
    expects_reply: bool = False,
    reply_via: str = '',
    mirrored_from: str = '',
) -> str:
    body = str(message or '').strip()
    label = message_label(kind, system_relay=system_relay)
    source = str(source_node_id or '').strip()
    header = f'{label}，ID是{source}，内容为：' if source else f'{label}，内容为：'
    if not body:
        return header
    text = f'{header}\n{body}'
    if reply_via == 'chatbox':
        return f'{text}\n{CHATBOX_REPLY_TAIL}'
    if expects_reply:
        return f'{text}\n{FRONTDOOR_REPLY_TAIL}'
    if str(mirrored_from or '').strip():
        return f'{text}\n{ACCEPTANCE_MIRROR_TAIL}'
    return text


def strip_node_message_prefix(text: Any) -> str:
    """Return the raw body when ``text`` is a rendered mailbox message.

    Only the exact one-line header is removed. A real user message that merely
    mentions a node in its body is left untouched.
    """
    value = str(text or '').strip()
    if not value:
        return ''
    first, separator, remainder = value.partition('\n')
    if separator and _PREFIX_RE.match(first.strip()):
        return remainder.strip()
    return value


__all__ = [
    'message_label',
    'render_node_message_text',
    'strip_node_message_prefix',
]
