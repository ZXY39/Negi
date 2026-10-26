"""Task-node mailbox message classification.

``kind`` and ``priority`` are **derived**, never declared by the sender. The
sender only picks the addressee and an intent word; everything that决定
delivery and wake behaviour is computed here from the endpoint relationship and
whether the runtime generated the message.

Why this is a pure module: the whole point of deriving instead of declaring is
that a sender cannot mislabel a message to wake a parked node, nor label an
ordinary notice as a terminal result. That guarantee only holds if there is
exactly one place that computes it.

Priority rule (design invariant P6) — the runtime needs the recipient to act:

* a human / front-door / abnormal pause / a ``reject_no_progress`` verdict
  wakes the recipient unconditionally;
* every ordinary node-authored message is ``normal``. The recipient's own
  ``enter_wait`` (always "any") decides whether normal mail starts a turn.
"""

from __future__ import annotations

from typing import Final

KIND_USER_DIRECT: Final = 'user_direct'
KIND_FRONTDOOR_NOTICE: Final = 'frontdoor_notice'
KIND_PARENT_NOTICE: Final = 'parent_notice'
KIND_CHILD_PROGRESS: Final = 'child_progress'
KIND_CHILD_FINAL: Final = 'child_final'
KIND_CHILD_ABNORMAL_PAUSE: Final = 'child_abnormal_pause'
KIND_ACCEPTANCE_VERDICT: Final = 'acceptance_verdict'
KIND_PEER_NOTICE: Final = 'peer_notice'
# `schedule_wake` 到点时由运行时投给自己的唤醒。它只能是运行时报的：模型若能用
# `send_message` 造出这一档，就等于能给自己发一条"看起来是定时器响了"的假消息。
KIND_WAKE_TIMER: Final = 'wake_timer'

ORIGIN_USER: Final = 'user'
ORIGIN_FRONTDOOR: Final = 'frontdoor'
ORIGIN_NODE: Final = 'node'
ORIGIN_SYSTEM: Final = 'system'

RELATION_PARENT: Final = 'parent'
RELATION_CHILD: Final = 'child'
RELATION_PEER: Final = 'peer'

PRIORITY_HIGH: Final = 'high'
PRIORITY_NORMAL: Final = 'normal'

VERDICT_PASS: Final = 'pass'
VERDICT_REJECT: Final = 'reject'
VERDICT_REJECT_NO_PROGRESS: Final = 'reject_no_progress'
VERDICTS: Final = frozenset({VERDICT_PASS, VERDICT_REJECT, VERDICT_REJECT_NO_PROGRESS})

# 只能由运行时产出、模型无法通过 send_message 伪造的四类。
SYSTEM_KINDS: Final = frozenset(
    {KIND_CHILD_FINAL, KIND_CHILD_ABNORMAL_PAUSE, KIND_ACCEPTANCE_VERDICT, KIND_WAKE_TIMER}
)

# 无条件唤醒的 kind：收件人必须动作，否则系统或发送方无法推进（P6）。
ALWAYS_WAKING_KINDS: Final = frozenset(
    {KIND_USER_DIRECT, KIND_FRONTDOOR_NOTICE, KIND_CHILD_ABNORMAL_PAUSE}
)


class MessageClassificationError(ValueError):
    """Sender-described fields cannot produce a valid message."""


def derive_message_kind(
    *,
    origin: str,
    relation: str = '',
    system_kind: str = '',
) -> str:
    """Return the mailbox ``kind`` for a message.

    ``system_kind`` short-circuits: only the runtime may pass it, and it names
    one of :data:`SYSTEM_KINDS`. Everything else is decided by the origin and
    the endpoint relationship, so a model-sent message can never claim to be a
    terminal result or a verdict.

    ``relation`` 是**发送方相对收件人的角色**：发给父节点是 ``child``，
    发给子节点/后代是 ``parent``，发给同级是 ``peer``。服务层按
    ``parent_node_id`` 现算，模型无法声明。
    """
    normalized_origin = str(origin or '').strip().lower()
    normalized_system = str(system_kind or '').strip()
    if normalized_system:
        if normalized_system not in SYSTEM_KINDS:
            raise MessageClassificationError(f'unknown_system_kind:{normalized_system}')
        return normalized_system
    if normalized_origin == ORIGIN_USER:
        return KIND_USER_DIRECT
    if normalized_origin == ORIGIN_FRONTDOOR:
        return KIND_FRONTDOOR_NOTICE
    if normalized_origin != ORIGIN_NODE:
        raise MessageClassificationError(f'unknown_origin:{normalized_origin or "<empty>"}')
    normalized_relation = str(relation or '').strip().lower()
    if normalized_relation == RELATION_PARENT:
        return KIND_PARENT_NOTICE
    if normalized_relation == RELATION_CHILD:
        return KIND_CHILD_PROGRESS
    if normalized_relation == RELATION_PEER:
        return KIND_PEER_NOTICE
    raise MessageClassificationError(f'unknown_relation:{normalized_relation or "<empty>"}')


def derive_priority(
    *,
    kind: str,
    verdict: str = '',
) -> str:
    """Return ``high`` when the message must wake the recipient regardless of
    its ``enter_wait`` policy, ``normal`` otherwise."""
    normalized_kind = str(kind or '').strip()
    if not normalized_kind:
        raise MessageClassificationError('kind_required')
    if normalized_kind in ALWAYS_WAKING_KINDS:
        return PRIORITY_HIGH
    if normalized_kind == KIND_ACCEPTANCE_VERDICT:
        # 驳回且判为无进展会把被验节点送入暂停·abnormal，父节点必须动作。
        return (
            PRIORITY_HIGH
            if str(verdict or '').strip() == VERDICT_REJECT_NO_PROGRESS
            else PRIORITY_NORMAL
        )
    return PRIORITY_NORMAL


def validate_verdict(verdict: str) -> str:
    normalized = str(verdict or '').strip()
    if normalized not in VERDICTS:
        raise MessageClassificationError(f'unknown_verdict:{normalized or "<empty>"}')
    return normalized


def classify(
    *,
    origin: str,
    relation: str = '',
    system_kind: str = '',
    verdict: str = '',
) -> tuple[str, str]:
    """Convenience: derive ``(kind, priority)`` in one call."""
    kind = derive_message_kind(origin=origin, relation=relation, system_kind=system_kind)
    priority = derive_priority(kind=kind, verdict=verdict)
    return kind, priority
