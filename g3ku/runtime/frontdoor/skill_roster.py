"""skill 名单的按轮检测与注入台账。

头部钉住块带的是**钉住时点**的全量可见 skill（见 `tool_contract.render_pinned_contract_text`），
两次重印之间可见集怎么变头部都不动。这里补上另一半：

- 每轮把 live 可见集与"头部名单 ∪ 已注入台账"做差，差集非空就生成
  `new_readable_skills` 注入块（id | 名称 | 描述），追加在尾部契约块里；
- 台账**只增不减**：skill 一旦被点名过就一直记着，权限收回/删除不会把块缩回去——
  模型侧对该 skill 的最终裁决来自 `load_skill_context` 的三态回执；
- 块每轮从台账整体重渲染，阶段裁撤（`context_evicted`）不可能把它丢掉——
  它不依赖历史里的任何一份拷贝，这比走 `kept_*` 继承搬移便宜且无漏；
- 台账条目一旦被重印后的头部名单覆盖，就从注入块里摘除（信息仍在头部，块不无限涨）。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

_LEDGER_STORE_LIMIT = 256
_LEDGERS: 'OrderedDict[str, set[str]]' = OrderedDict()

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


def _ledger_for(scope_key: str) -> set[str]:
    key = _normalize_id(scope_key)
    if not key:
        raise ValueError('skill roster ledger requires a scope key')
    ledger = _LEDGERS.get(key)
    if ledger is None:
        ledger = set()
        _LEDGERS[key] = ledger
        _LEDGERS.move_to_end(key)
    while len(_LEDGERS) > _LEDGER_STORE_LIMIT:
        _LEDGERS.popitem(last=False)
    return ledger


def injected_skill_ids(scope_key: str) -> set[str]:
    key = _normalize_id(scope_key)
    if not key:
        return set()
    return set(_LEDGERS.get(key) or set())


def clear_skill_roster_ledgers() -> None:
    """测试隔离：台账是进程级的，跨用例可见。"""
    _LEDGERS.clear()


def new_readable_skill_items(
    *,
    scope_key: str,
    live_visible_skills: list[Any],
    covered_skill_ids: list[Any] | None,
) -> list[dict[str, str]]:
    """本轮注入块的内容：live 可见 −（头部名单 ∪ 已注入台账）。

    差集非空时把新名字记入台账并返回条目；差集为空返回空列表，尾块不出这一段。
    台账写入发生在读取之后：同一轮内重复装配拿到同一份块（幂等），下一轮不再重复。
    """
    key = _normalize_id(scope_key)
    if not key:
        return []
    covered = {
        _normalize_id(item)
        for item in list(covered_skill_ids or [])
        if _normalize_id(item)
    }
    ledger = _ledger_for(key)
    fresh: list[dict[str, str]] = []
    for item in list(live_visible_skills or []):
        normalized = normalize_skill_roster_item(item)
        if normalized is None:
            continue
        skill_id = normalized['skill_id']
        if skill_id in covered or skill_id in ledger:
            continue
        ledger.add(skill_id)
        fresh.append(normalized)
    if fresh:
        _LEDGERS.move_to_end(key)
    return fresh


def render_new_readable_skills_lines(items: list[dict[str, str]] | None) -> list[str]:
    """注入块的渲染行；空列表返回空（尾块整段省略）。"""
    lines: list[str] = []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        entry = normalize_skill_roster_item(item)
        if entry is None or entry['skill_id'] in seen:
            continue
        seen.add(entry['skill_id'])
        normalized.append(entry)
    if not normalized:
        return lines
    lines.append(
        'new_readable_skills (重印点之后新变为可读，可直接 `load_skill_context`; `id | name | description`):'
    )
    lines.extend(
        f'- `{item["skill_id"]}` | {item["display_name"]} | {item["description"]}'
        for item in normalized
    )
    return lines


__all__ = [
    'clear_skill_roster_ledgers',
    'injected_skill_ids',
    'new_readable_skill_items',
    'normalize_skill_roster_item',
    'render_new_readable_skills_lines',
]
