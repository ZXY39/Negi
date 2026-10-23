"""Task-node mailbox message classification.

``kind`` and ``priority`` are **derived**, never declared by the sender. The
sender only picks the addressee and an intent word; everything that决定
delivery and wake behaviour is computed here from the endpoint relationship and
whether the runtime generated the message.

Why this is a pure module: the whole point of deriving instead of declaring is
that a sender cannot mislabel a message to wake a parked node, nor label an
ordinary notice as a terminal result. That guarantee only holds if there is
exactly one place that computes it.

Priority rule (design invariant P6) — "does the sender park because of this
message", not "what type is it":

* a sender that keeps working after sending (progress / instruction)  -> normal
* a sender that parks waiting for an answer (``intent_kind=请求``)      -> high
* the runtime needs the recipient to act (abnormal pause, user, front door,
  and a ``reject_no_progress`` verdict)                                -> high
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
# 预留：跨非父子节点的投递。地址空间已经就位，本期只在投递前加一道父子关系校验。
KIND_PEER_NOTICE: Final = 'peer_notice'

ORIGIN_USER: Final = 'user'
ORIGIN_FRONTDOOR: Final = 'frontdoor'
ORIGIN_NODE: Final = 'node'
ORIGIN_SYSTEM: Final = 'system'

RELATION_PARENT: Final = 'parent'
RELATION_CHILD: Final = 'child'

INTENT_PROGRESS: Final = '进展'
INTENT_REQUEST: Final = '请求'
INTENT_INSTRUCTION: Final = '指令'
INTENT_KINDS: Final = frozenset({INTENT_PROGRESS, INTENT_REQUEST, INTENT_INSTRUCTION})

PRIORITY_HIGH: Final = 'high'
PRIORITY_NORMAL: Final = 'normal'

VERDICT_PASS: Final = 'pass'
VERDICT_REJECT: Final = 'reject'
VERDICT_REJECT_NO_PROGRESS: Final = 'reject_no_progress'
VERDICTS: Final = frozenset({VERDICT_PASS, VERDICT_REJECT, VERDICT_REJECT_NO_PROGRESS})

# 只能由运行时产出、模型无法通过 send_message 伪造的三类。
SYSTEM_KINDS: Final = frozenset(
    {KIND_CHILD_FINAL, KIND_CHILD_ABNORMAL_PAUSE, KIND_ACCEPTANCE_VERDICT}
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
    raise MessageClassificationError(f'unknown_relation:{normalized_relation or "<empty>"}')


def derive_priority(
    *,
    kind: str,
    intent_kind: str = '',
    verdict: str = '',
) -> str:
    """Return ``high`` when the message must wake the recipient regardless of
    its ``wake_on`` policy, ``normal`` otherwise."""
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
    if normalized_kind in {KIND_PARENT_NOTICE, KIND_CHILD_PROGRESS}:
        return (
            PRIORITY_HIGH
            if str(intent_kind or '').strip() == INTENT_REQUEST
            else PRIORITY_NORMAL
        )
    return PRIORITY_NORMAL


def validate_intent_kind(intent_kind: str) -> str:
    normalized = str(intent_kind or '').strip()
    if not normalized:
        return INTENT_PROGRESS
    if normalized not in INTENT_KINDS:
        raise MessageClassificationError(f'unknown_intent_kind:{normalized}')
    return normalized


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
    intent_kind: str = '',
    verdict: str = '',
) -> tuple[str, str]:
    """Convenience: derive ``(kind, priority)`` in one call."""
    kind = derive_message_kind(origin=origin, relation=relation, system_kind=system_kind)
    priority = derive_priority(kind=kind, intent_kind=intent_kind, verdict=verdict)
    return kind, priority
