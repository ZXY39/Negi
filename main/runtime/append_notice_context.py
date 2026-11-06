from __future__ import annotations

import json
from typing import Any

from main.runtime.node_message_text import strip_node_message_prefix

APPEND_NOTICE_CONTEXT_KEY = 'append_notice_context'
PENDING_APPEND_NOTICE_RECORDS_KEY = 'pending_append_notice_records'
APPEND_NOTICE_TAIL_PREFIX = '[NOTICE_TAIL]'
# 存量节点台账与已装配消息里仍是旧拼写：读侧都认，写侧只出新拼写。
APPEND_NOTICE_TAIL_V1_PREFIX = '[NOTICE_TAIL_V1]'
LEGACY_APPEND_NOTICE_TAIL_PREFIX = '[G3KU_APPEND_NOTICE_TAIL_V1]'
APPEND_NOTICE_TAIL_PREFIXES = (
    APPEND_NOTICE_TAIL_PREFIX,
    APPEND_NOTICE_TAIL_V1_PREFIX,
    LEGACY_APPEND_NOTICE_TAIL_PREFIX,
)

# 尾块 live 窗口：已并入过的通知只保留最近 N 条，其余在压缩边界退场。
# 台账（notice_records）全量保留，供 view_message_history / content_search 检索。
NODE_NOTICE_CONSUMED_TAIL_WINDOW = 5
NOTICE_INDEX_PREFIX = '[NOTICE_INDEX]'

# 定向通知转述族：系统把用户追加的通知转述给祖先/验收节点时生成的包装消息。
# 新记录在通知行的 payload 里带 origin 标记；存量行用消息前缀兜底识别。
NOTICE_ORIGIN_SYSTEM_RELAY = 'system_relay'
SYSTEM_RELAY_NOTICE_PREFIXES = (
    '【任务收到用户定向通知】',
    '【后代节点收到用户定向通知】',
    '【被检验节点收到用户定向通知】',
    '【正在检验的节点接收了用户通知】',
)


def is_system_relay_notice(record: Any) -> bool:
    """该通知是否属于「定向通知转述族」（消息列表展示时过滤，不影响模型上下文）。

    判定源：通知行 payload.origin == system_relay（新记录），或 message 以
    任一已知转述前缀开头（兼容存量数据行，两者只影响展示层）。
    """
    payload = record.get('payload') if isinstance(record, dict) else getattr(record, 'payload', None)
    if isinstance(payload, dict) and str(payload.get('origin') or '').strip() == NOTICE_ORIGIN_SYSTEM_RELAY:
        return True
    message = str(record.get('message') if isinstance(record, dict) else getattr(record, 'message', '') or '').strip()
    return message.startswith(SYSTEM_RELAY_NOTICE_PREFIXES)


def normalize_append_notice_context(payload: Any) -> dict[str, Any]:
    current = dict(payload or {}) if isinstance(payload, dict) else {}
    notice_records: list[dict[str, Any]] = []
    seen_notice_ids: set[str] = set()
    for item in list(current.get('notice_records') or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if not notification_id or notification_id in seen_notice_ids:
            continue
        seen_notice_ids.add(notification_id)
        notice_records.append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'kind': str(item.get('kind') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'received_at': str(item.get('received_at') or '').strip(),
                'consumed_at': str(item.get('consumed_at') or '').strip(),
                'merged_at': str(item.get('merged_at') or '').strip(),
                'compression_stage_id': str(item.get('compression_stage_id') or '').strip(),
                'superseded_at': str(item.get('superseded_at') or '').strip(),
            }
        )
    compression_segments: list[dict[str, Any]] = []
    seen_segment_ids: set[str] = set()
    for item in list(current.get('compression_segments') or []):
        if not isinstance(item, dict):
            continue
        compression_stage_id = str(item.get('compression_stage_id') or '').strip()
        if not compression_stage_id or compression_stage_id in seen_segment_ids:
            continue
        seen_segment_ids.add(compression_stage_id)
        notice_ids = [
            str(notice_id or '').strip()
            for notice_id in list(item.get('notice_ids') or [])
            if str(notice_id or '').strip()
        ]
        compression_segments.append(
            {
                'compression_stage_id': compression_stage_id,
                'created_at': str(item.get('created_at') or '').strip(),
                'notice_ids': notice_ids,
                'notice_count': max(0, int(item.get('notice_count') or len(notice_ids))),
                'summary_text': str(item.get('summary_text') or '').strip(),
            }
        )
    return {
        'notice_records': notice_records,
        'compression_segments': compression_segments,
    }


def normalize_pending_append_notice_records(payload: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_notice_ids: set[str] = set()
    for item in list(payload or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if not notification_id or notification_id in seen_notice_ids:
            continue
        seen_notice_ids.add(notification_id)
        records.append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'created_at': str(item.get('created_at') or '').strip(),
                'order_index': max(0, int(item.get('order_index') or 0)),
                # 控制/决策回合已处理（显示「已消费」），内容等待恢复路径并入。
                'processed_at': str(item.get('processed_at') or '').strip(),
            }
        )
    records.sort(
        key=lambda item: (
            str(item.get('created_at') or ''),
            int(item.get('order_index') or 0),
            str(item.get('notification_id') or ''),
        )
    )
    return records


def record_pending_append_notice_records(
    payload: Any,
    *,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = normalize_pending_append_notice_records(payload)
    existing_ids = {
        str(item.get('notification_id') or '').strip()
        for item in list(normalized or [])
    }
    for item in list(records or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if not notification_id or notification_id in existing_ids:
            continue
        existing_ids.add(notification_id)
        normalized.append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'created_at': str(item.get('created_at') or '').strip(),
                'order_index': max(0, int(item.get('order_index') or 0)),
                'processed_at': str(item.get('processed_at') or '').strip(),
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item.get('created_at') or ''),
            int(item.get('order_index') or 0),
            str(item.get('notification_id') or ''),
        )
    )
    return normalized


def consume_pending_append_notice_records(
    payload: Any,
    *,
    notification_ids: list[str],
) -> list[dict[str, Any]]:
    consumed_ids = {
        str(item or '').strip()
        for item in list(notification_ids or [])
        if str(item or '').strip()
    }
    if not consumed_ids:
        return normalize_pending_append_notice_records(payload)
    return [
        dict(item)
        for item in list(normalize_pending_append_notice_records(payload) or [])
        if str(item.get('notification_id') or '').strip() not in consumed_ids
    ]


def record_consumed_notifications(
    context: Any,
    *,
    notifications: list[dict[str, Any]],
    consumed_at: str,
    merged_at: str = '',
) -> dict[str, Any]:
    normalized = normalize_append_notice_context(context)
    existing_ids = {str(item.get('notification_id') or '').strip() for item in list(normalized.get('notice_records') or [])}
    normalized_merged_at = str(merged_at or '').strip()
    for item in list(notifications or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if not notification_id or notification_id in existing_ids:
            continue
        existing_ids.add(notification_id)
        normalized['notice_records'].append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'kind': str(item.get('kind') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'received_at': str(
                    item.get('received_at')
                    or item.get('delivered_at')
                    or item.get('created_at')
                    or consumed_at
                    or ''
                ).strip(),
                'consumed_at': str(item.get('consumed_at') or consumed_at or '').strip(),
                # 三态：merged_at 为空 = 已消费（内容稍后并入或不再并入）。
                'merged_at': str(item.get('merged_at') or normalized_merged_at or '').strip(),
                'compression_stage_id': '',
                'superseded_at': str(item.get('superseded_at') or '').strip(),
            }
        )
    return normalized


def supersede_append_notice_records(
    context: Any,
    *,
    notification_ids: list[str] | None = None,
    source_node_id: str = '',
    superseded_at: str,
) -> dict[str, Any]:
    """Mark matching notice records as superseded so they stop resurfacing.

    A superseded record stays in the context for forensics but is excluded from
    the raw notice tail window and from compression-segment rollups: its
    directive was already answered (e.g. a block-verification cycle resolved,
    or a newer handoff message from the same source replaced it).
    """
    normalized = normalize_append_notice_context(context)
    normalized_at = str(superseded_at or '').strip()
    if not normalized_at:
        return normalized
    wanted_ids = {
        str(item or '').strip()
        for item in list(notification_ids or [])
        if str(item or '').strip()
    }
    normalized_source = str(source_node_id or '').strip()
    if not wanted_ids and not normalized_source:
        return normalized
    next_records: list[dict[str, Any]] = []
    for item in list(normalized.get('notice_records') or []):
        payload = dict(item or {})
        if not str(payload.get('superseded_at') or '').strip():
            matched = str(payload.get('notification_id') or '').strip() in wanted_ids
            if not matched and normalized_source:
                matched = str(payload.get('source_node_id') or '').strip() == normalized_source
            if matched:
                payload['superseded_at'] = normalized_at
        next_records.append(payload)
    normalized['notice_records'] = next_records
    return normalized


def roll_append_notice_context_for_compression_stage(
    context: Any,
    *,
    compression_stage_id: str,
    created_at: str,
) -> dict[str, Any]:
    normalized = normalize_append_notice_context(context)
    normalized_stage_id = str(compression_stage_id or '').strip()
    if not normalized_stage_id:
        return normalized
    if any(str(item.get('compression_stage_id') or '').strip() == normalized_stage_id for item in list(normalized.get('compression_segments') or [])):
        return normalized
    # 只代持还在尾窗里的通知：已消费且超出窗口的记录随压缩退场，
    # 不再另立新段（否则每条历史通知都会变成永久占位的逐字段）。
    live_ids = live_append_notice_ids(normalized)
    uncovered = [
        item
        for item in list(normalized.get('notice_records') or [])
        if not str(item.get('compression_stage_id') or '').strip()
        and not str(item.get('superseded_at') or '').strip()
        and str(item.get('message') or '').strip()
        and str(item.get('notification_id') or '').strip() in live_ids
    ]
    if not uncovered:
        return normalized
    summary_lines = []
    for item in uncovered:
        consumed_at = str(item.get('consumed_at') or '').strip()
        message = str(item.get('message') or '').strip()
        if not message:
            continue
        if consumed_at:
            summary_lines.append(f'- [{consumed_at}] {message}')
        else:
            summary_lines.append(f'- {message}')
    summary_text = '\n'.join(summary_lines).strip()
    normalized['compression_segments'].append(
        {
            'compression_stage_id': normalized_stage_id,
            'created_at': str(created_at or '').strip(),
            'notice_ids': [str(item.get('notification_id') or '').strip() for item in uncovered],
            'notice_count': len(uncovered),
            'summary_text': summary_text,
        }
    )
    next_notice_records: list[dict[str, Any]] = []
    covered_ids = {str(item.get('notification_id') or '').strip() for item in uncovered}
    for item in list(normalized.get('notice_records') or []):
        payload = dict(item or {})
        if str(payload.get('notification_id') or '').strip() in covered_ids:
            payload['compression_stage_id'] = normalized_stage_id
        next_notice_records.append(payload)
    normalized['notice_records'] = next_notice_records
    return normalized


APPEND_NOTICE_MUST_PRESERVE_HEADING = '【必须逐条保留的追加要求】'


def _notice_record_is_consumed(record: dict[str, Any]) -> bool:
    # merged_at 是"真正并入过上下文"；存量记录缺它时用 consumed_at 兜底。
    return bool(str(record.get('merged_at') or record.get('consumed_at') or '').strip())


def _notice_record_sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return (
        str(
            record.get('received_at')
            or record.get('consumed_at')
            or record.get('merged_at')
            or ''
        ).strip(),
        str(record.get('notification_id') or '').strip(),
    )


def select_append_notice_tail_records(
    context: Any,
    *,
    consumed_window: int = NODE_NOTICE_CONSUMED_TAIL_WINDOW,
) -> list[dict[str, Any]]:
    """尾块 live 集合：未消费 + 最近 N 条已消费；superseded / 空正文排除。

    台账本身不裁剪：这里只决定哪些记录还需要每跳重投。已消费且落在窗口外的
    记录由 view_message_history / content_search 按需检索。
    """
    normalized = normalize_append_notice_context(context)
    records = [
        item
        for item in list(normalized.get('notice_records') or [])
        if str(item.get('message') or '').strip()
        and not str(item.get('superseded_at') or '').strip()
    ]
    consumed = [item for item in records if _notice_record_is_consumed(item)]
    keep_ids = {
        str(item.get('notification_id') or '').strip()
        for item in sorted(consumed, key=_notice_record_sort_key)[
            -max(0, int(consumed_window or 0)) :
        ]
    }
    live: list[dict[str, Any]] = []
    for item in records:
        notification_id = str(item.get('notification_id') or '').strip()
        if _notice_record_is_consumed(item) and notification_id not in keep_ids:
            continue
        live.append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'consumed_at': str(item.get('consumed_at') or '').strip(),
            }
        )
    return live


def live_append_notice_ids(
    context: Any,
    *,
    consumed_window: int = NODE_NOTICE_CONSUMED_TAIL_WINDOW,
) -> set[str]:
    return {
        item['notification_id']
        for item in select_append_notice_tail_records(
            context,
            consumed_window=consumed_window,
        )
        if item['notification_id']
    }


def archived_consumed_notice_count(
    context: Any,
    *,
    consumed_window: int = NODE_NOTICE_CONSUMED_TAIL_WINDOW,
) -> int:
    """已消费但已退出尾块窗口的记录数（压缩边界退场的那一批）。"""
    normalized = normalize_append_notice_context(context)
    live_ids = live_append_notice_ids(
        normalized,
        consumed_window=consumed_window,
    )
    return sum(
        1
        for item in list(normalized.get('notice_records') or [])
        if str(item.get('message') or '').strip()
        and not str(item.get('superseded_at') or '').strip()
        and str(item.get('notification_id') or '').strip() not in live_ids
    )


def is_notice_index_message(message: Any) -> bool:
    content = str((message or {}).get('content') or '').strip()
    return content.startswith(f'{NOTICE_INDEX_PREFIX}\n')


def build_notice_index_message(
    context: Any,
    *,
    consumed_window: int = NODE_NOTICE_CONSUMED_TAIL_WINDOW,
) -> dict[str, Any] | None:
    """一条极小的尾索引：告诉模型有多少已消费通知已退出上下文、怎么取回。

    只报告数量与最老时间，不重投正文；没有退场记录时不产行。
    """
    normalized = normalize_append_notice_context(context)
    live_ids = live_append_notice_ids(
        normalized,
        consumed_window=consumed_window,
    )
    archived = [
        item
        for item in list(normalized.get('notice_records') or [])
        if str(item.get('message') or '').strip()
        and not str(item.get('superseded_at') or '').strip()
        and str(item.get('notification_id') or '').strip() not in live_ids
    ]
    if not archived:
        return None
    oldest = min(
        (
            _notice_record_sort_key(item)[0]
            for item in archived
            if _notice_record_sort_key(item)[0]
        ),
        default='',
    )
    lines = [
        NOTICE_INDEX_PREFIX,
        f'Older consumed notices outside the live window: {len(archived)}'
        + (f' (oldest {oldest}).' if oldest else '.'),
        'They may not be in this request; use view_message_history to list them, then '
        'content_search(ref=..., query=...) or content_open(ref=...) to read one.',
    ]
    return {
        'role': 'user',
        'content': '\n'.join(lines),
        # 不给 internal_prompt_kind：那个键会把消息归到 hidden_internal_event 家族。
        'metadata': {'ui_visible': False},
    }


def select_uncovered_append_notices(
    context: Any,
    *,
    consumed_window: int = NODE_NOTICE_CONSUMED_TAIL_WINDOW,
) -> list[dict[str, Any]]:
    """live 窗口里还没被摘要段代持的通知原文。

    尾块 raw 窗口与压缩「必须保留段」共用这一条判据；已被代持的记录由
    `compressed_notice_window` 段承载，不重复进 raw 窗口。
    """
    normalized = normalize_append_notice_context(context)
    covered_ids = {
        str(item.get('notification_id') or '').strip()
        for item in list(normalized.get('notice_records') or [])
        if str(item.get('compression_stage_id') or '').strip()
    }
    return [
        item
        for item in select_append_notice_tail_records(
            normalized,
            consumed_window=consumed_window,
        )
        if item['notification_id'] not in covered_ids
    ]


def prune_append_notice_context_segments(
    context: Any,
    *,
    live_notice_ids: set[str] | None = None,
    consumed_window: int = NODE_NOTICE_CONSUMED_TAIL_WINDOW,
) -> dict[str, Any]:
    """压缩边界：丢掉只含非 live 通知的代持段；notice_records 全量保留。

    带 `notice_ids` 的段按交集判；没有 `notice_ids` 的存量段无法判断，保守保留。
    """
    normalized = normalize_append_notice_context(context)
    live_ids = (
        set(live_notice_ids)
        if live_notice_ids is not None
        else live_append_notice_ids(normalized, consumed_window=consumed_window)
    )
    kept: list[dict[str, Any]] = []
    for segment in list(normalized.get('compression_segments') or []):
        notice_ids = {
            str(item or '').strip()
            for item in list(segment.get('notice_ids') or [])
            if str(item or '').strip()
        }
        if notice_ids and not (notice_ids & live_ids):
            continue
        kept.append(segment)
    normalized['compression_segments'] = kept
    return normalized


def _notice_anchor(message: Any) -> str:
    return ''.join(str(message or '').split())[:24]


def render_append_notice_must_preserve_block(records: list[dict[str, Any]]) -> str:
    """把未代持要求钉进压缩指令尾部。

    压缩指令的任务目标取的是节点建任务时的 goal，摘要器按"是否有助于该目标"筛选，
    中途改方式的要求会被判成过程信息、甚至反写成"一直是这么做的"既成事实。
    """
    lines = []
    for item in list(records or []):
        if not isinstance(item, dict):
            continue
        message = str(item.get('message') or '').strip()
        if not message:
            continue
        consumed_at = str(item.get('consumed_at') or '').strip()
        prefix = f'[{consumed_at}] ' if consumed_at else ''
        lines.append(f'- {prefix}{message}')
    if not lines:
        return ''
    return f'{APPEND_NOTICE_MUST_PRESERVE_HEADING}\n' + '\n'.join(lines)


def score_append_notice_preservation(summary_text: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """要点命中读数：每条取正文归一化后前 24 字作锚。

    启发式，只用于长期档位判断——锚不在摘要里可以断定"没带上"，命中不等于语义完整。
    """
    normalized_summary = ''.join(str(summary_text or '').split())
    hits = 0
    miss_ids: list[str] = []
    for item in list(records or []):
        if not isinstance(item, dict):
            continue
        anchor = _notice_anchor(item.get('message'))
        if not anchor:
            continue
        if anchor in normalized_summary:
            hits += 1
        else:
            miss_ids.append(str(item.get('notification_id') or '').strip())
    return {
        'open_count': len(list(records or [])),
        'hit_count': hits,
        'miss_count': len(list(records or [])) - hits,
        'miss_notification_ids': [item for item in miss_ids if item],
    }


def append_notice_tail_prefix_match(message: Any) -> str:
    """命中的块前缀，没命中回空串。

    块形状固定是「前缀 + 换行 + JSON」，正文里提到前缀不算块；识别不挑角色，
    因为存量块带 assistant 角色而新块按线体合同以 user 落地。
    """
    content = str((message or {}).get('content') or '').strip()
    for prefix in APPEND_NOTICE_TAIL_PREFIXES:
        if content.startswith(f'{prefix}\n'):
            return prefix
    return ''


def is_append_notice_tail_message(message: Any) -> bool:
    """该消息是不是运行时装进去的通知块（代持段或未消费窗口）。"""
    return bool(append_notice_tail_prefix_match(message))


def append_notice_window_notice_ids(message: Any) -> set[str]:
    """从 `raw_notice_window` 块里读通知 id；解析失败返回空集。

    压缩切分靠这个判据决定深埋窗口要不要捞出：空集按"未知"处理，调用方保守保留。
    """
    prefix = append_notice_tail_prefix_match(message)
    if not prefix:
        return set()
    content = str((message or {}).get('content') or '').strip()
    try:
        payload = json.loads(content[len(prefix) :].strip())
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()
    ids: set[str] = set()
    for item in list(payload.get('notices') or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if notification_id:
            ids.add(notification_id)
    return ids


def dedupe_append_notice_tail_messages(
    candidates: list[dict[str, Any]],
    existing_messages: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """装配期只放"历史里还没有的那一份"块，重复的直接不插。

    块是 JSON 且 `sort_keys=True`，同一条代持内容逐字节相同；而装配结果会被回写进节点
    账本（`frame.messages`），每跳再插一次就每跳叠一份——实盘一条账本 135 帧里叠了 36 份
    同样的 245 字符块。这里挡新副本、不回删已沉淀的历史帧：删掉已经发出去的帧等于重写
    provider 前缀，代价比留几份重复块大。
    """
    seen = {
        _append_notice_block_key(item)
        for item in list(existing_messages or [])
        if is_append_notice_tail_message(item)
    }
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in list(candidates or []):
        if not isinstance(item, dict):
            continue
        key = _append_notice_block_key(item)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(dict(item))
    return kept, dropped


def _append_notice_block_key(message: Any) -> str:
    """按去掉前缀后的正文比块：换拼写那一次跳转，存量旧块与新块同内容只算一份。"""
    content = str((message or {}).get('content') or '').strip()
    prefix = append_notice_tail_prefix_match(message)
    if not prefix:
        return content
    return APPEND_NOTICE_TAIL_PREFIX + content[len(prefix):]


def build_append_notice_tail_messages(
    context: Any,
    *,
    visible_user_messages: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_append_notice_context(context)
    live_records = select_append_notice_tail_records(normalized)
    live_ids = {
        str(item.get('notification_id') or '').strip()
        for item in live_records
        if str(item.get('notification_id') or '').strip()
    }
    visible_texts = {
        strip_node_message_prefix(item)
        for item in list(visible_user_messages or [])
        if str(item or '').strip()
    }
    messages: list[dict[str, Any]] = []
    for segment in list(normalized.get('compression_segments') or []):
        segment_notice_ids = {
            str(item or '').strip()
            for item in list(segment.get('notice_ids') or [])
            if str(item or '').strip()
        }
        # 只含已退场通知的旧段不再渲染；没有 notice_ids 的存量段保守保留。
        if segment_notice_ids and not (segment_notice_ids & live_ids):
            continue
        payload = {
            'kind': 'compressed_notice_window',
            'compression_stage_id': str(segment.get('compression_stage_id') or '').strip(),
            'notice_count': int(segment.get('notice_count') or 0),
            'summary_text': str(segment.get('summary_text') or '').strip(),
        }
        messages.append(
            {
                'role': 'user',
                'content': f'{APPEND_NOTICE_TAIL_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}',
            }
        )
    raw_notices = [
        dict(item)
        for item in select_uncovered_append_notices(normalized)
        if str(item.get('message') or '').strip() not in visible_texts
    ]
    if raw_notices:
        payload = {
            'kind': 'raw_notice_window',
            'notice_count': len(raw_notices),
            'notices': raw_notices,
        }
        messages.append(
            {
                'role': 'user',
                'content': f'{APPEND_NOTICE_TAIL_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}',
            }
        )
    return messages


__all__ = [
    'APPEND_NOTICE_CONTEXT_KEY',
    'PENDING_APPEND_NOTICE_RECORDS_KEY',
    'APPEND_NOTICE_TAIL_PREFIX',
    'APPEND_NOTICE_TAIL_PREFIXES',
    'LEGACY_APPEND_NOTICE_TAIL_PREFIX',
    'NODE_NOTICE_CONSUMED_TAIL_WINDOW',
    'NOTICE_INDEX_PREFIX',
    'append_notice_tail_prefix_match',
    'append_notice_window_notice_ids',
    'archived_consumed_notice_count',
    'build_notice_index_message',
    'is_notice_index_message',
    'live_append_notice_ids',
    'NOTICE_ORIGIN_SYSTEM_RELAY',
    'SYSTEM_RELAY_NOTICE_PREFIXES',
    'is_system_relay_notice',
    'build_append_notice_tail_messages',
    'consume_pending_append_notice_records',
    'normalize_append_notice_context',
    'normalize_pending_append_notice_records',
    'prune_append_notice_context_segments',
    'record_pending_append_notice_records',
    'record_consumed_notifications',
    'roll_append_notice_context_for_compression_stage',
    'select_append_notice_tail_records',
    'supersede_append_notice_records',
    'APPEND_NOTICE_MUST_PRESERVE_HEADING',
    'dedupe_append_notice_tail_messages',
    'is_append_notice_tail_message',
    'render_append_notice_must_preserve_block',
    'score_append_notice_preservation',
    'select_uncovered_append_notices',
]
