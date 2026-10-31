"""Model-facing description budgets for resources.

A tool's function description and a skill's manifest description are the
always-present bytes in the provider request (or the pinned roster).  Keep
them to one sentence: details belong to the toolskill / SKILL.md body, which
is loaded on demand.

The cap applies to resources added after a manager's baseline exists; the
baseline is initialized from the resources present on first load so existing
manifests are grandfathered instead of turning every long-lived skill into a
repair-required resource overnight.  New and re-added resources must fit.
"""

from __future__ import annotations

TOOL_DESCRIPTION_MAX_CHARS = 160
SKILL_DESCRIPTION_MAX_CHARS = 160


def description_limit_for(kind: str) -> int:
    return TOOL_DESCRIPTION_MAX_CHARS if str(kind).lower() == 'tool' else SKILL_DESCRIPTION_MAX_CHARS


def description_too_long_error(kind: str, text: str, *, limit: int | None = None) -> str:
    """Stable, model-facing failure text for an over-budget description."""
    cap = int(limit or description_limit_for(kind))
    length = len(str(text or ''))
    return (
        f'description_too_long: {kind} description is {length} characters '
        f'(limit {cap}); rewrite it as one sentence. Resource was not added.'
    )


__all__ = [
    'SKILL_DESCRIPTION_MAX_CHARS',
    'TOOL_DESCRIPTION_MAX_CHARS',
    'description_limit_for',
    'description_too_long_error',
]
