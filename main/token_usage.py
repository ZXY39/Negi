from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from g3ku.providers.base import LLMModelAttempt


def _snapshot_cls():
    from main.models import TokenUsageSummary

    return TokenUsageSummary


def _model_snapshot_cls():
    from main.models import ModelTokenUsageRecord

    return ModelTokenUsageRecord


def empty_token_usage(*, tracked: bool = False):
    return _snapshot_cls()(tracked=tracked)


def known_total_tokens(record) -> int:
    return int(getattr(record, 'input_tokens', 0) or 0) + int(getattr(record, 'output_tokens', 0) or 0)


def merge_token_usage_records(records: Iterable[object], *, tracked: bool | None = None):
    tracked_flag = bool(tracked)
    summary = empty_token_usage(tracked=tracked_flag)
    seen = False
    for record in records:
        if record is None:
            continue
        seen = True
        if tracked is None and bool(getattr(record, 'tracked', False)):
            tracked_flag = True
            summary.tracked = True
        summary.input_tokens += int(getattr(record, 'input_tokens', 0) or 0)
        summary.output_tokens += int(getattr(record, 'output_tokens', 0) or 0)
        summary.cache_hit_tokens += int(getattr(record, 'cache_hit_tokens', 0) or 0)
        summary.call_count += int(getattr(record, 'call_count', 0) or 0)
        summary.calls_with_usage += int(getattr(record, 'calls_with_usage', 0) or 0)
        summary.calls_without_usage += int(getattr(record, 'calls_without_usage', 0) or 0)
    if tracked is None and not seen:
        tracked_flag = False
    summary.tracked = tracked_flag
    summary.is_partial = bool(summary.calls_without_usage > 0) if tracked_flag else False
    return summary


def merge_token_usage_by_model(records: Iterable[object], *, tracked: bool | None = None):
    ModelTokenUsageRecord = _model_snapshot_cls()
    grouped: dict[tuple[str, str, str], list[object]] = defaultdict(list)
    for record in records:
        if record is None:
            continue
        key = (
            str(getattr(record, 'model_key', '') or '').strip(),
            str(getattr(record, 'provider_id', '') or '').strip(),
            str(getattr(record, 'provider_model', '') or '').strip(),
        )
        grouped[key].append(record)
    merged: list[object] = []
    for (model_key, provider_id, provider_model), items in grouped.items():
        summary = merge_token_usage_records(items, tracked=tracked)
        merged.append(
            ModelTokenUsageRecord(
                model_key=model_key,
                provider_id=provider_id,
                provider_model=provider_model,
                tracked=summary.tracked,
                input_tokens=summary.input_tokens,
                output_tokens=summary.output_tokens,
                cache_hit_tokens=summary.cache_hit_tokens,
                call_count=summary.call_count,
                calls_with_usage=summary.calls_with_usage,
                calls_without_usage=summary.calls_without_usage,
                is_partial=summary.is_partial,
                # 负载均衡归属是**成员身份**，不是计数：同一颗成员在这一跳里被试多次也只
                # 属于同一个组/同一格，所以取首个非哨兵值，不求和也不平均。整组都是哨兵
                # 就是"这一发没有租约"（前门/辅助车道），不要拿 0 去填。
                route_index=_first_declared(items, 'route_index', -1),
                group_key=_first_declared(items, 'group_key', ''),
                quota_bucket_index=_first_declared(items, 'quota_bucket_index', -1),
            )
        )
    merged.sort(key=lambda item: (-known_total_tokens(item), str(getattr(item, 'model_key', '') or '')))
    return merged


def _first_declared(items: list[object], field_name: str, sentinel):
    """取第一个不等于哨兵的值；全为哨兵则返回哨兵。"""
    for item in items:
        value = getattr(item, field_name, sentinel)
        if value != sentinel:
            return value
    return sentinel


def _as_slot(value: object) -> int:
    """链上格号/桶序号：非数值或负值收敛成哨兵 -1（= 没有租约，不是第 0 格）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1
    numeric = int(value)
    return numeric if numeric >= 0 else -1


def build_token_usage_from_attempts(attempts: Iterable[LLMModelAttempt], *, tracked: bool):
    ModelTokenUsageRecord = _model_snapshot_cls()
    if not tracked:
        return empty_token_usage(tracked=False), []

    records: list[object] = []
    for attempt in attempts:
        usage = dict(getattr(attempt, 'usage', {}) or {})
        has_usage = bool(usage)
        records.append(
            ModelTokenUsageRecord(
                model_key=str(getattr(attempt, 'model_key', '') or '').strip(),
                provider_id=str(getattr(attempt, 'provider_id', '') or '').strip(),
                provider_model=str(getattr(attempt, 'provider_model', '') or '').strip(),
                tracked=True,
                input_tokens=int(usage.get('input_tokens', 0) or 0),
                output_tokens=int(usage.get('output_tokens', 0) or 0),
                cache_hit_tokens=int(usage.get('cache_hit_tokens', 0) or 0),
                call_count=1,
                calls_with_usage=1 if has_usage else 0,
                calls_without_usage=0 if has_usage else 1,
                is_partial=not has_usage,
                route_index=_as_slot(getattr(attempt, 'route_index', -1)),
                group_key=str(getattr(attempt, 'group_key', '') or '').strip(),
                quota_bucket_index=_as_slot(getattr(attempt, 'quota_bucket_index', -1)),
            )
        )
    merged = merge_token_usage_by_model(records, tracked=True)
    return merge_token_usage_records(merged, tracked=True), merged


def aggregate_node_token_usage(nodes: Iterable[object], *, tracked: bool):
    if not tracked:
        return empty_token_usage(tracked=False), []
    node_list = list(nodes)
    by_model_records = [
        entry
        for node in node_list
        for entry in list(getattr(node, 'token_usage_by_model', []) or [])
    ]
    return (
        merge_token_usage_records((getattr(node, 'token_usage', None) for node in node_list), tracked=True),
        merge_token_usage_by_model(by_model_records, tracked=True),
    )
