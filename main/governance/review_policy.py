"""CEO 前门审批档位与免审工具名单的持久化形状。

两个资源服务（``MainRuntimeService`` 与 admin REST 的独立服务）读同一份 governance meta，
默认名单的派生规则只能有一处：各写一份会让「操作员从没存过名单」时的免审集合在两条车道上
不同形。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

GOVERNANCE_MODE_META_KEY = 'ceo_frontdoor_regulatory_mode_enabled'
GOVERNANCE_MODE_UPDATED_AT_META_KEY = 'ceo_frontdoor_regulatory_mode_enabled:updated_at'
GOVERNANCE_REVIEW_WHITELIST_META_KEY = 'ceo_frontdoor_tool_review_whitelist'

REVIEW_MODE_ALL_PASS = 'all_pass'
REVIEW_MODE_CONDITIONAL = 'conditional'
REVIEW_FREE_DEFAULT_RISK_LEVEL = 'low'


def review_mode_from_enabled(enabled: bool) -> str:
    return REVIEW_MODE_CONDITIONAL if bool(enabled) else REVIEW_MODE_ALL_PASS


def normalize_review_tool_ids(values: Iterable[Any] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        tool_id = str(raw or '').strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        normalized.append(tool_id)
    return normalized


def default_review_free_tool_ids(records: Iterable[Any] | None) -> list[str]:
    tool_ids = [
        str(getattr(record, 'tool_id', '') or '').strip()
        for record in list(records or [])
        if str(getattr(record, 'risk_level', '') or '').strip().lower() == REVIEW_FREE_DEFAULT_RISK_LEVEL
    ]
    return normalize_review_tool_ids(tool_ids)


def read_stored_review_free_tool_ids(store: Any) -> list[str] | None:
    """``None`` = 从没保存过名单，调用方按风险度派生默认档；空列表是操作员主动清空。"""
    raw = store.get_meta(GOVERNANCE_REVIEW_WHITELIST_META_KEY)
    if raw is None or not str(raw).strip():
        return None
    payload = json.loads(raw)
    return normalize_review_tool_ids(payload if isinstance(payload, list) else [])


def resolve_review_free_tool_ids(store: Any, records: Iterable[Any] | None) -> tuple[list[str], bool]:
    """返回（生效的免审名单，是否来自操作员自定义）。"""
    stored = read_stored_review_free_tool_ids(store)
    if stored is None:
        return default_review_free_tool_ids(records), False
    return stored, True


def write_review_free_tool_ids(store: Any, tool_ids: Iterable[Any] | None) -> list[str]:
    normalized = normalize_review_tool_ids(tool_ids)
    store.set_meta(GOVERNANCE_REVIEW_WHITELIST_META_KEY, json.dumps(normalized))
    return normalized
