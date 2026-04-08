"""Structured memory helpers (runtime v1).

Task 2 scope: this module provides a normalized in-memory representation of a
structured fact plus a few small helper utilities used by the memory runtime.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any, Literal, get_args


StructuredCategory = Literal[
    "identity",
    "preference",
    "constraint",
    "workflow_rule",
    "default_setting",
    "stateful_fact",
    "historical_fact",
    "relationship",
]

TimeSemantics = Literal[
    "durable_until_replaced",
    "current_state",
    "historical_observation",
]


@dataclass(frozen=True, slots=True)
class StructuredMemoryFact:
    fact_id: str
    category: StructuredCategory
    scope: str
    entity: str
    attribute: str
    value: Any
    observed_at: str
    time_semantics: TimeSemantics
    source_excerpt: str | None
    qualifier: dict[str, Any] | None
    expires_at: str | None
    canonical_key: str
    statement: str
    created_at: str
    updated_at: str


def _norm_token(value: object) -> str:
    token = str(value or "").strip()
    return " ".join(token.split()).lower()


def canonical_key_for_fact(fact: StructuredMemoryFact) -> str:
    """Return a stable key for deduping facts across writes.

    Intentionally ignores `fact_id`, timestamps, and free-form text fields.
    """

    # Keep this human-readable (helps debugging) while remaining stable.
    # Order matters and should only change on a deliberate schema bump.
    parts = (
        _norm_token(fact.scope),
        _norm_token(fact.category),
        _norm_token(fact.entity),
        _norm_token(fact.attribute),
        _norm_token(fact.time_semantics),
    )
    return "|".join(part or "_" for part in parts)


def render_statement(fact: StructuredMemoryFact) -> str:
    """Return a user-facing statement for the fact."""

    base = str(fact.statement or "").strip()
    if not base:
        rendered_value = fact.value
        if isinstance(rendered_value, (dict, list)):
            rendered_value = str(rendered_value)
        base = f"{fact.entity}.{fact.attribute} = {rendered_value}"

    if fact.time_semantics == "current_state" and fact.observed_at:
        if fact.observed_at not in base:
            base = f"{base} (observed_at={fact.observed_at})"
    return base


def normalize_fact(raw: dict[str, Any], *, fact_id: str, now_iso: str) -> StructuredMemoryFact:
    """Normalize a raw dict into a StructuredMemoryFact.

    This is deliberately permissive: it is used on partially-formed payloads and
    fills in sensible defaults until full reconciliation logic lands.
    """

    category_raw = raw.get("category")
    if isinstance(category_raw, str) and category_raw in get_args(StructuredCategory):
        category: StructuredCategory = category_raw  # type: ignore[assignment]
    elif raw.get("stateful_fact") is True:
        category = "stateful_fact"
    elif raw.get("historical_fact") is True:
        category = "historical_fact"
    else:
        category = "historical_fact"

    time_raw = raw.get("time_semantics")
    if isinstance(time_raw, str) and time_raw in get_args(TimeSemantics):
        time_semantics: TimeSemantics = time_raw  # type: ignore[assignment]
    elif category == "stateful_fact":
        time_semantics = "current_state"
    elif category == "historical_fact":
        time_semantics = "historical_observation"
    else:
        time_semantics = "durable_until_replaced"

    scope = str(raw.get("scope") or "session")
    entity = str(raw.get("entity") or raw.get("subject") or "self")
    attribute = str(raw.get("attribute") or raw.get("slot_id") or raw.get("key") or "").strip()

    value: Any
    if "value" in raw:
        value = raw.get("value")
    else:
        state = raw.get("state")
        if isinstance(state, dict) and "value" in state:
            value = state.get("value")
        else:
            value = raw.get("rendered_statement") or raw.get("statement") or ""

    observed_at = str(raw.get("observed_at") or raw.get("timestamp") or now_iso)

    source_excerpt = raw.get("source_excerpt")
    if source_excerpt is not None:
        source_excerpt = str(source_excerpt)

    qualifier = raw.get("qualifier")
    if qualifier is not None and not isinstance(qualifier, dict):
        qualifier = None

    expires_at = raw.get("expires_at")
    if expires_at is not None:
        expires_at = str(expires_at)

    statement = str(raw.get("statement") or raw.get("rendered_statement") or "").strip()

    created_at = str(raw.get("created_at") or now_iso)
    updated_at = str(raw.get("updated_at") or now_iso)

    provisional = StructuredMemoryFact(
        fact_id=str(fact_id),
        category=category,
        scope=scope,
        entity=entity,
        attribute=attribute,
        value=value,
        observed_at=observed_at,
        time_semantics=time_semantics,
        source_excerpt=source_excerpt,
        qualifier=qualifier,  # type: ignore[arg-type]
        expires_at=expires_at,
        canonical_key=str(raw.get("canonical_key") or ""),
        statement=statement,
        created_at=created_at,
        updated_at=updated_at,
    )

    canonical_key = provisional.canonical_key or canonical_key_for_fact(provisional)
    final_statement = statement or render_statement(provisional)

    return StructuredMemoryFact(
        fact_id=provisional.fact_id,
        category=provisional.category,
        scope=provisional.scope,
        entity=provisional.entity,
        attribute=provisional.attribute,
        value=provisional.value,
        observed_at=provisional.observed_at,
        time_semantics=provisional.time_semantics,
        source_excerpt=provisional.source_excerpt,
        qualifier=provisional.qualifier,
        expires_at=provisional.expires_at,
        canonical_key=canonical_key,
        statement=final_statement,
        created_at=provisional.created_at,
        updated_at=provisional.updated_at,
    )


def equivalent_fact(left: StructuredMemoryFact, right: StructuredMemoryFact) -> bool:
    """Return True if the facts should be treated as duplicates."""

    if left.canonical_key != right.canonical_key:
        # Fall back to content-based matching if canonical_key is missing.
        if canonical_key_for_fact(left) != canonical_key_for_fact(right):
            return False

    if left.time_semantics != right.time_semantics:
        return False

    # For current-state slots, the observation timestamp participates in dedupe.
    # Otherwise a later observation with the same value would get dropped.
    if left.time_semantics == "current_state":
        if (left.observed_at or "") != (right.observed_at or ""):
            return False

    if left.value != right.value:
        return False

    if (left.qualifier or None) != (right.qualifier or None):
        return False

    if (left.expires_at or None) != (right.expires_at or None):
        return False

    return True


def fact_to_metadata(fact: StructuredMemoryFact) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "memory_format": "structured_v1",
        "fact_id": fact.fact_id,
        "category": fact.category,
        "scope": fact.scope,
        "entity": fact.entity,
        "attribute": fact.attribute,
        "observed_at": fact.observed_at,
        "time_semantics": fact.time_semantics,
        "expires_at": fact.expires_at,
        "canonical_key": fact.canonical_key,
        "created_at": fact.created_at,
        "updated_at": fact.updated_at,
    }
    if fact.source_excerpt:
        meta["source_excerpt"] = fact.source_excerpt
    if fact.qualifier:
        meta["qualifier"] = fact.qualifier
    return meta


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except Exception:
        return None


def replacement_required(old: StructuredMemoryFact, new: StructuredMemoryFact) -> bool:
    """Return True if `new` should replace `old` in the active set.

    The runtime currently only performs deterministic replacement for "current_state"
    slots (stateful facts). Other categories may be handled via merges later.
    """

    if old.canonical_key != new.canonical_key:
        return False

    # Never "replace" with an equivalent fact; treat it as a noop.
    if equivalent_fact(old, new):
        return False

    if old.time_semantics != "current_state" or new.time_semantics != "current_state":
        return False

    old_ts = _parse_iso(old.observed_at)
    new_ts = _parse_iso(new.observed_at)
    if old_ts is not None and new_ts is not None:
        if new_ts > old_ts:
            return True
        if new_ts < old_ts:
            return False
    else:
        # Fall back to lexicographic ordering for deterministic behavior.
        if str(new.observed_at or "") > str(old.observed_at or ""):
            return True
        if str(new.observed_at or "") < str(old.observed_at or ""):
            return False

    # Tie-break deterministically when observed_at matches or parsing failed.
    if str(new.updated_at or "") > str(old.updated_at or ""):
        return True
    if str(new.updated_at or "") < str(old.updated_at or ""):
        return False
    return str(new.fact_id or "") > str(old.fact_id or "")


def merge_required(old: StructuredMemoryFact, new: StructuredMemoryFact) -> bool:
    """Return True if `new` should be merged into `old` rather than replaced.

    Task 3 scope: no structured merge semantics are required for the runtime tests.
    This hook exists so Task 4 can add category-specific merges deterministically.
    """

    _ = old, new
    return False
