"""Delivery rules for the node mailbox.

Two things live here and nowhere else:

1. **the delivery matrix** — which target states accept which sender. Terminal
   targets accept nothing (terminal is absorbing) and a paused target refuses a
   human's free text while still accepting a machine's message, because the
   machine has no "resume" button: for it the message *is* the handling path.
2. **the fingerprint** — dedupe key for retries of the same message.

Both are pure so the matrix can be read in one place instead of being
reconstructed from scattered guards.
"""

from __future__ import annotations

import hashlib
from typing import Final

PARK_TERMINAL: Final = 'terminal'
PARK_PAUSED: Final = 'paused'
PARK_WAITING: Final = 'waiting'
PARK_RUNNING: Final = 'running'

REJECT_TARGET_TERMINAL: Final = 'target_terminal'
REJECT_TARGET_PAUSED_NEEDS_RESUME: Final = 'target_paused_requires_resume'
REJECT_UNKNOWN_TARGET_STATE: Final = 'unknown_target_state'
# 寻址层的两个拒收档，必须分开：`target_not_in_task` 要模型改地址，
# `target_list_unparsed` 要它改**形状**（它想发的是列表，只是文本没解析成列表）。
# 合成一个值就只能两样都猜一遍。
REJECT_TARGET_NOT_IN_TASK: Final = 'target_not_in_task'
REJECT_TARGET_LIST_UNPARSED: Final = 'target_list_unparsed'
# 形状档附给模型的那一句。措辞只说本条 `to` 接受什么，不指任何全局设置。
TARGET_LIST_UNPARSED_HINT: Final = (
    'Pass `to` as one node id, or as a real JSON list of node ids.'
)

# 用户自由文本对暂停态拒收；机器发送方照常投递并激活（设计 §2.4 注）。
_USER_ORIGINS = frozenset({'user'})


def plan_delivery(*, target_park_state: str, sender_origin: str) -> tuple[bool, str]:
    """Return ``(accepted, reject_reason)`` for one targeted delivery.

    ``target_park_state`` comes from :func:`main.runtime.node_wake.node_park_state`
    so the matrix and the wake decision agree on what "paused" and "terminal" mean.
    """
    state = str(target_park_state or '').strip().lower()
    origin = str(sender_origin or '').strip().lower()
    if state in {PARK_TERMINAL}:
        # P3: 终态吸收。回执由调用方负责，这里只给判定。
        return False, REJECT_TARGET_TERMINAL
    if state == PARK_PAUSED and origin in _USER_ORIGINS:
        return False, REJECT_TARGET_PAUSED_NEEDS_RESUME
    if state in {PARK_PAUSED, PARK_WAITING, PARK_RUNNING}:
        return True, ''
    return False, REJECT_UNKNOWN_TARGET_STATE


def message_fingerprint(
    *,
    kind: str,
    source_node_id: str,
    message: str,
    ref: str = '',
) -> str:
    """Stable dedupe key for one logical message.

    Deliberately excludes the addressee: a fan-out to five siblings is five
    deliveries of one message and each mailbox dedupes against itself. It also
    excludes timestamps, so a retry of the same send collapses onto the first.
    """
    digest = hashlib.sha256(
        '\n'.join(
            (
                str(kind or '').strip(),
                str(source_node_id or '').strip(),
                str(message or '').strip(),
                str(ref or '').strip(),
            )
        ).encode('utf-8')
    ).hexdigest()
    return digest[:16]


def notification_is_held(record: object) -> bool:
    """True while a batch-held child_final must not be injected or wake anyone."""
    if isinstance(record, dict):
        held_at = record.get('held_at')
        released_at = record.get('released_at')
    else:
        held_at = getattr(record, 'held_at', '')
        released_at = getattr(record, 'released_at', '')
    return bool(str(held_at or '').strip()) and not str(released_at or '').strip()


def normalize_targets(raw: object) -> list[str]:
    """Flatten ``to`` into an ordered, de-duplicated target list.

    Accepts one id/sentinel, or a list of them; order is preserved so the
    per-target results array lines up with what the caller asked for.
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or '').strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def unresolved_target_reason(raw: object) -> str:
    """Name why one requested target was not accepted, past any shape repair.

    A value that still opens like a container means the model asked for a list and the
    text did not parse; anything else is a token that simply names no node of this task.
    Those need different fixes, so they must not share one rejection value.
    """
    text = str(raw or '').strip()
    if text[:1] in ('[', '{'):
        return REJECT_TARGET_LIST_UNPARSED
    return REJECT_TARGET_NOT_IN_TASK


# 节点不带"改模型链 / 管环境"的能力，所以一条错误消息能给的唯一有效出路是把它交上去。
# 这句话按收件人能不能直达前门分两档：只有根节点的 `send_message` 接受 `frontdoor`，
# 对非根节点印那句就是在指一条会被 `frontdoor_root_only` 拒掉的死路。
def error_escalation_hint(*, receiver_is_root: bool) -> str:
    """The one line appended to error-class mailbox messages telling the receiver where to escalate."""
    if receiver_is_root:
        return (
            'If you cannot resolve this, report it to the front door: '
            '`send_message(to="frontdoor", content=...)`. Model chains, provider keys and '
            'runtime configuration are not yours to change — say what you observed and let the '
            'front door decide.'
        )
    return (
        'If you cannot resolve this, pass it up: `send_message(to="parent", content=...)` — only '
        'the root node can reach the front door, so name the failing node id and what you tried. '
        'Model chains, provider keys and runtime configuration are not yours to change.'
    )
