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
            )
        )
    merged.sort(key=lambda item: (-known_total_tokens(item), str(getattr(item, 'model_key', '') or '')))
    return merged


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
