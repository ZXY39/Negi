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
    KIND_CHILD_ABNORMAL_PAUSE,
    KIND_CHILD_FINAL,
    KIND_CHILD_PROGRESS,
    KIND_FRONTDOOR_NOTICE,
    KIND_PARENT_NOTICE,
    KIND_PEER_NOTICE,
    KIND_USER_DIRECT,
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
}

_PREFIX_RE = re.compile(
    r'^来自(?:父节点|子节点|同级节点|验收节点|会话|用户|系统)'
    r'(?:，ID是[^，\n]+)?，内容为：$'
)


def message_label(kind: str, *, system_relay: bool = False) -> str:
    if system_relay:
        return '来自系统'
    return _LABEL_BY_KIND.get(str(kind or '').strip(), '来自系统')


def render_node_message_text(
    *,
    kind: str,
    source_node_id: str = '',
    message: str,
    system_relay: bool = False,
) -> str:
    body = str(message or '').strip()
    label = message_label(kind, system_relay=system_relay)
    source = str(source_node_id or '').strip()
    header = f'{label}，ID是{source}，内容为：' if source else f'{label}，内容为：'
    return f'{header}\n{body}' if body else header


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
