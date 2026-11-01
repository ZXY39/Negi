"""Operator-action feed for model-facing runtime notices.

Every console mutation that changes what a model can see (stickers, long-term
memory, tools, skills, persona, session tier/tool list) appends one row here.
The runtime notice lane consumes the feed through a per-scope cursor and renders
a `### Operator actions (from the console)` section, so the model learns *who*
changed the state instead of inferring it from a value diff.

The feed is a separate table rather than provenance on the state rows: the same
state diff can be produced by the model itself (editing a skill file, committing
memory through the memory agent), and labelling every diff as operator-caused
would be wrong. Only the console write path appends here.

Retention is bounded: a session that has been idle past the window simply aligns
its cursor to the latest row and does not replay history.
"""

from __future__ import annotations

from typing import Any

AUDIENCE_FRONTDOOR = "frontdoor"
AUDIENCE_NODE = "node"
AUDIENCE_ALL = "all"
OPERATOR_ACTION_AUDIENCES = (AUDIENCE_FRONTDOOR, AUDIENCE_NODE, AUDIENCE_ALL)

SCOPE_GLOBAL = ""

# One notice may carry at most this many action lines; the rest stay for the
# next turn because the cursor only advances past what was rendered.
MAX_ACTIONS_PER_NOTICE = 20
# Rows kept for late readers; older rows are pruned on append.
RETENTION_ROWS = 2000


def session_scope(session_key: Any) -> str:
    key = str(session_key or "").strip()
    return f"session:{key}" if key else SCOPE_GLOBAL


def memory_scope(scope: Any) -> str:
    key = str(scope or "").strip()
    return f"memory:group:{key}" if key else "memory:local"


def format_operator_action(action: str) -> str:
    """One model-facing line body, English like every other runtime notice.

    "user (operator)" is deliberate: front-door prompts define the operator as
    the user, while node prompts do not, and the attribution has to be readable
    in every lane.
    """
    text = " ".join(str(action or "").split()).rstrip(". ")
    if not text:
        raise ValueError("operator action text is required")
    return f"The user (operator) {text} from the console."


def scope_keys_for_frontdoor(session_key: Any, *, group_key: str = "") -> tuple[str, ...]:
    """Scopes a front-door session can see.

    Local memory is read by every session (group sessions include it), group
    memory only by the matching group, and tier/tool-list changes only by the
    session they were made on.
    """
    keys = [SCOPE_GLOBAL, session_scope(session_key), memory_scope("")]
    normalized_group = str(group_key or "").strip()
    if normalized_group:
        keys.append(memory_scope(normalized_group))
    ordered: list[str] = []
    for key in keys:
        if key not in ordered:
            ordered.append(key)
    return tuple(ordered)


def scope_keys_for_node() -> tuple[str, ...]:
    return (SCOPE_GLOBAL,)


__all__ = [
    "AUDIENCE_ALL",
    "AUDIENCE_FRONTDOOR",
    "AUDIENCE_NODE",
    "MAX_ACTIONS_PER_NOTICE",
    "OPERATOR_ACTION_AUDIENCES",
    "RETENTION_ROWS",
    "SCOPE_GLOBAL",
    "format_operator_action",
    "memory_scope",
    "scope_keys_for_frontdoor",
    "scope_keys_for_node",
    "session_scope",
]
