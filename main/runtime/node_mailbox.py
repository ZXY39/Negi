"""Delivery rules for the node mailbox.

Three things live here and nowhere else:

1. **the delivery matrix** — which target states accept which sender. Terminal
   targets accept nothing (terminal is absorbing); everything else lands in the
   mailbox, including a human's free text to a paused node.
2. **the wake consequence** — a paused node is not woken by that human message,
   only by the operator's resume button. A machine's message *does* wake it,
   because the machine has no resume button: for it the message is the handling
   path.
3. **the fingerprint** — dedupe key for retries of the same message.

All three are pure so the matrix can be read in one place instead of being
reconstructed from scattered guards.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

PARK_TERMINAL: Final = 'terminal'
PARK_PAUSED: Final = 'paused'
PARK_WAITING: Final = 'waiting'
PARK_RUNNING: Final = 'running'

REJECT_TARGET_TERMINAL: Final = 'target_terminal'
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
TASK_TARGET_ROOT_NOTICE: Final = (
    'Task id accepted; delivered to the task root node. The root decides whether and '
    'how to carry it down to its children.'
)

# 用户自由文本是唯一「落箱但不叫醒」的发送方，其余发送方照常被唤醒。
_USER_ORIGINS = frozenset({'user'})


def plan_delivery(*, target_park_state: str, sender_origin: str) -> tuple[bool, str]:
    """Return ``(accepted, reject_reason)`` for one targeted delivery.

    ``target_park_state`` comes from :func:`main.runtime.node_wake.node_park_state`
    so the matrix and the wake decision agree on what "paused" and "terminal" mean.
    Only a terminal target refuses anything, and no sender can talk its way past
    that: ``sender_origin`` deliberately plays no part in acceptance — it decides
    only the wake consequence, in :func:`delivery_defers_wake`.
    """
    state = str(target_park_state or '').strip().lower()
    if state in {PARK_TERMINAL}:
        # P3: 终态吸收。回执由调用方负责，这里只给判定。
        return False, REJECT_TARGET_TERMINAL
    if state in {PARK_PAUSED, PARK_WAITING, PARK_RUNNING}:
        return True, ''
    return False, REJECT_UNKNOWN_TARGET_STATE


def delivery_defers_wake(*, target_park_state: str, sender_origin: str) -> bool:
    """True when the message lands as unconsumed mail instead of reviving the node.

    A human addressing a paused node is answering the pause, not lifting it: waking
    would clear the pause flag through ``wake_parked_node``, so sending a message
    would silently resume what the operator stopped. The row stays unread until the
    resume button, and the node's next turn folds it in like any other mail.
    """
    state = str(target_park_state or '').strip().lower()
    origin = str(sender_origin or '').strip().lower()
    return state == PARK_PAUSED and origin in _USER_ORIGINS


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


def normalize_address_token(raw: object) -> str:
    """Repair the spelling variants a model may put in a target field.

    Whitespace around the value or the namespace separator, a full-width colon,
    and doubled separators all name the same address; none of them should become
    ``target_not_in_task``.
    """
    text = str(raw or '').strip()
    if not text:
        return ''
    text = text.replace('\uff1a', ':')
    text = re.sub(r'\s*:\s*', ':', text)
    text = re.sub(r':+', ':', text)
    return re.sub(r'\s+', '', text)


def split_address_target(raw: object) -> tuple[str, str]:
    """Return ``(namespace, value)`` for one target token.

    The namespace is lowercase and empty for a bare id. A token that carries an
    unrecognised namespace keeps it, so an id from the wrong namespace is never
    read as a bare id by mistake.
    """
    token = normalize_address_token(raw)
    prefix, separator, value = token.partition(':')
    if not separator:
        return '', token
    prefix = prefix.strip().lower()
    value = value.strip()
    if not prefix or not value:
        return 'invalid', token
    return prefix, value


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
# 这句话按收件人分两档，但分的不是"谁能到前门"（任何执行节点都能），而是**一条分支的故障
# 该由谁汇总**：错误继续交父节点，一次故障只上去一条；前门自己提问要求回复的那一句走
# `to="frontdoor"`，见 `main/runtime/node_message_text.py` 的回复义务句。
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
        'If you cannot resolve this, pass it up: `send_message(to="parent", content=...)` — your '
        'parent holds this branch, so name the failing node id and what you tried and let it '
        'decide whether to take it further. '
        'Model chains, provider keys and runtime configuration are not yours to change.'
    )
