"""Model-facing description budgets for resources.

A tool's function description and a skill's manifest description are the
always-present bytes in the provider request (or the pinned roster).  Keep
them to one sentence: details belong to the toolskill / SKILL.md body, which
is loaded on demand.

The cap applies to resources added after a manager's baseline exists; the
baseline is initialized from the resources present on first load so existing
manifests are grandfathered instead of turning every long-lived skill into a
repair-required resource overnight.  New and re-added resources must fit.

The rejection has to name the carrier it measured.  A tool's measured text is
the loaded module's own `model_description` / `description`, not the manifest's
`description:` — and the repair projection shown in `tools[]` prints the
manifest value, so a refusal that only reports a length sends the model back to
the file it has already shortened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

TOOL_DESCRIPTION_MAX_CHARS = 160
SKILL_DESCRIPTION_MAX_CHARS = 160

# 被拒文本必然超上限，全文回贴会让这条拒绝自己变成一个新的长块。预览上限直接取同一种
# 资源的预算本身，不另立一个会漂移的数字。
_OFFENDING_TEXT_SUFFIX = '...(truncated)'


def description_limit_for(kind: str) -> int:
    return TOOL_DESCRIPTION_MAX_CHARS if str(kind).lower() == 'tool' else SKILL_DESCRIPTION_MAX_CHARS


def description_carrier(path: Any, field: str, *, base: Any = None) -> str:
    """Render `<posix path>:<field>` — where the measured description actually lives.

    Paths are made relative to the manager's workspace when possible so the model
    can open them from the repo root instead of resolving an absolute host path.
    """
    raw = str(path or '').strip()
    cleaned_field = str(field or '').strip()
    if not raw:
        return cleaned_field
    resolved = Path(raw)
    prefix = str(base or '').strip()
    if prefix:
        try:
            resolved = resolved.relative_to(Path(prefix))
        except ValueError:
            pass
    return f'{resolved.as_posix()}:{cleaned_field}'


def description_too_long_error(kind: str, text: str, *, limit: int | None = None, source: str = '') -> str:
    """Stable, model-facing failure text for an over-budget description.

    Three readable readings, in one line: the numbers, the carrier that was
    measured, and a bounded preview of the offending text.
    """
    cap = int(limit or description_limit_for(kind))
    description = str(text or '').strip()
    length = len(description)
    preview = description if length <= cap else f'{description[:cap]}{_OFFENDING_TEXT_SUFFIX}'
    lead = (
        f'description_too_long: {kind} description is {length} characters '
        f'(limit {cap}); rewrite it as one sentence. Resource was not added.'
    )
    if source:
        return f'{lead} Measured text lives in {source}. Offending text: "{preview}"'
    return f'{lead} Offending text: "{preview}"'


__all__ = [
    'SKILL_DESCRIPTION_MAX_CHARS',
    'TOOL_DESCRIPTION_MAX_CHARS',
    'description_carrier',
    'description_limit_for',
    'description_too_long_error',
]
