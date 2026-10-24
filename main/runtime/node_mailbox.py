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

from main.runtime.node_wake import (
    WAKE_ON_ALL_CHILDREN,
    WAKE_ON_ANY,
)

PARK_TERMINAL: Final = 'terminal'
PARK_PAUSED: Final = 'paused'
PARK_WAITING: Final = 'waiting'
PARK_RUNNING: Final = 'running'

REJECT_TARGET_TERMINAL: Final = 'target_terminal'
REJECT_TARGET_PAUSED_NEEDS_RESUME: Final = 'target_paused_requires_resume'
REJECT_UNKNOWN_TARGET_STATE: Final = 'unknown_target_state'

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


def normalize_wake_on(raw: object) -> str:
    value = str(raw or '').strip() or WAKE_ON_ANY
    if value not in {WAKE_ON_ANY, WAKE_ON_ALL_CHILDREN}:
        raise ValueError(f'unknown_wake_on:{value}')
    return value
