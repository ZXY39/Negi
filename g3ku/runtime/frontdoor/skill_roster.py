"""skill 名单的取样归一。

名单本身的载体合同见 `g3ku/runtime/frontdoor/tool_contract.py`（头部钉住块）与
`g3ku/runtime/skill_roster_notice.py`（重印点之间新增可读的一次性持久提示）。
这里只留"把治理记录归一成 roster item"这一件事——两车道共用，描述截断口径只写一遍。
"""

from __future__ import annotations

from typing import Any

_DESCRIPTION_MAX_CHARS = 160


def _normalize_id(value: Any) -> str:
    return str(value or '').strip()


def _record_field(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name) or '').strip()
    return str(getattr(item, name, '') or '').strip()


def _first_sentence(text: str) -> str:
    trimmed = ' '.join(str(text or '').split())
    if not trimmed:
        return ''
    for separator in ('。', '；', '. ', '; '):
        index = trimmed.find(separator)
        if index > 0:
            return trimmed[: index + len(separator)].rstrip()
    if '\n' in trimmed:
        trimmed = trimmed.split('\n', 1)[0].rstrip()
    if len(trimmed) > _DESCRIPTION_MAX_CHARS:
        return trimmed[: _DESCRIPTION_MAX_CHARS - 1].rstrip() + '…'
    return trimmed


def normalize_skill_roster_item(item: Any) -> dict[str, str] | None:
    skill_id = _record_field(item, 'skill_id')
    if not skill_id:
        return None
    display_name = _record_field(item, 'display_name') or skill_id
    description = _first_sentence(_record_field(item, 'l0') or _record_field(item, 'description'))
    return {
        'skill_id': skill_id,
        'display_name': display_name,
        'description': description,
    }


__all__ = [
    'normalize_skill_roster_item',
]
