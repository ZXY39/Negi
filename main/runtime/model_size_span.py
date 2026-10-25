"""Per-model send-size span learned in-process from accepted vs rejected hops.

A chain member can have a real request ceiling far below the `context_window_tokens`
its record declares (measured 2026-10-25: SenseNova-hosted members served 285k-token
bodies while the same session's other member rejected 287k with a generic
`inference request is invalid`). Nothing shrinks the body in that case, because the
compression ruler reads the declared window.

This module keeps the only evidence that can name the real ceiling: the largest
send-side size the member actually answered, and the smallest size it rejected. Both
arms are the **send-side estimate**, never provider-reported usage — the two yardsticks
differ by several percent and mixing them inverts the interval (that inversion is what
the first live attempt produced: accepted max 261,853 by usage vs rejected min 251,793
by estimate).

It reports and never acts: no routing change, no admission veto, no config write.
"""

from __future__ import annotations

import threading
from typing import Any, Final

# An inference needs at least one accepted hop below the rejected one. A 400 at a size
# the member has never served below that floor is not evidence of a ceiling: the same
# generic status also covers payload faults, so claiming a span there would teach the
# operator to distrust it.
_MIN_ACCEPTED_TOKENS: Final = 1024

_STATE_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}


def _key(model_key: str) -> str:
    return str(model_key or '').strip().lower()


def note_send_size(
    model_key: str,
    *,
    tokens: int,
    accepted: bool,
    declared_window_tokens: int = 0,
    compression_trigger_tokens: int = 0,
) -> dict[str, Any]:
    """Record one attempt's send size and return the span state after it.

    `changed` marks the first inference for this member and every material widening of
    it, which is what callers use to decide whether a fresh report line is owed.
    """
    key = _key(model_key)
    size = int(tokens or 0)
    if not key or size <= 0:
        return {"known": False, "changed": False, "model_key": key}
    with _STATE_LOCK:
        before = dict(_STATE.get(key) or {})
        state = _STATE.setdefault(
            key,
            {
                "accepted_max": 0,
                "rejected_min": 0,
                "rejected_count": 0,
                "declared_window_tokens": 0,
                "compression_trigger_tokens": 0,
            },
        )
        if accepted:
            if size > int(state.get("accepted_max") or 0):
                state["accepted_max"] = size
        else:
            current = int(state.get("rejected_min") or 0)
            state["rejected_count"] = int(state.get("rejected_count") or 0) + 1
            if current == 0 or size < current:
                state["rejected_min"] = size
        if int(declared_window_tokens or 0) > 0:
            state["declared_window_tokens"] = int(declared_window_tokens)
        if int(compression_trigger_tokens or 0) > 0:
            state["compression_trigger_tokens"] = int(compression_trigger_tokens)
        snapshot = dict(state)
        inferred = _inferable(snapshot)
        was_inferred = _inferable(before)
        widened = inferred and (
            not was_inferred
            or int(before.get("accepted_max") or 0) != int(snapshot.get("accepted_max") or 0)
            or int(before.get("rejected_min") or 0) != int(snapshot.get("rejected_min") or 0)
        )
        return {
            "known": inferred,
            "changed": bool(widened),
            "model_key": key,
            **snapshot,
        }


def _inferable(state: dict[str, Any]) -> bool:
    accepted_max = int((state or {}).get("accepted_max") or 0)
    rejected_min = int((state or {}).get("rejected_min") or 0)
    return accepted_max >= _MIN_ACCEPTED_TOKENS and rejected_min > accepted_max


def span_for(model_key: str) -> dict[str, Any]:
    """Return the inferred span, or `{}` while the evidence does not support one."""
    key = _key(model_key)
    if not key:
        return {}
    with _STATE_LOCK:
        state = dict(_STATE.get(key) or {})
    return state if _inferable(state) else {}


def describe_span(state: dict[str, Any]) -> str:
    """One-line report of the span, in the existing 'runtime does not edit config' voice."""
    if not _inferable(state):
        return ''
    accepted_max = int(state.get("accepted_max") or 0)
    rejected_min = int(state.get("rejected_min") or 0)
    width = rejected_min - accepted_max
    width_pct = round(width / accepted_max * 100, 1) if accepted_max else 0.0
    declared = int(state.get("declared_window_tokens") or 0)
    trigger = int(state.get("compression_trigger_tokens") or 0)
    model_key = str(state.get("model_key") or '').strip() or 'unknown'
    parts = [
        f'model_size_span: {model_key} answered {accepted_max:,} tokens and rejected '
        f'{rejected_min:,} (same send-side ruler; span width {width:,} = {width_pct}%). '
        'The real ceiling for this member sits inside that span, so a body above '
        f'{accepted_max:,} must not be assumed to fit it.'
    ]
    if declared:
        parts.append(
            f' Its record declares context_window_tokens={declared:,}'
            + (f' and the node compression trigger line is {trigger:,}' if trigger else '')
            + ' — both above the span, which is why nothing shrank this request.'
        )
    parts.append(
        f' Rejections counted: {int(state.get("rejected_count") or 0)}. '
        'Runtime does not change configuration: check that model record against the span, '
        'or report these numbers to the user.'
    )
    return ''.join(parts)


def reset_for_tests() -> None:
    with _STATE_LOCK:
        _STATE.clear()
