"""会话/节点存活期内新增可读 skill 的持久提示。

形状与维护者 2026-10-19 定的口径一致：提示**落进上下文、只注入一次**，不进每轮尾块；
压缩那一跳若该技能仍可读就把同一份名单带到压缩产物之后，不可读的在那一跳退场。

三条判据口径：

- 台账是唯一权威。"是否已经说过"读台账列，不扫渲染文本。
- 投递与检测同点、且**只有一个读者**（真正落请求体的那处装配点）。旧机制的失效原因
  就是一条回合内四个装配点共用一个消耗式读，第一个读者领走差集，上线那处永远拿空。
- 退场只发生在压缩跳——那一跳整份正文已被重写、可复用前缀反正已断，带一行不额外付。

两车道共用本模块的归一与渲染，字节同形（同 `render_candidate_tool_line` 的做法）。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from g3ku.runtime.frontdoor.skill_roster import normalize_skill_roster_item

SKILL_ROSTER_NOTICE_MARKER = '[SKILL_ROSTER_NOTICE_V1]'
SKILL_ROSTER_NOTICE_ROLE = 'user'
# 模型可见文案一律英文：这条提示会长期留在可复用前缀里，措辞只写"该做什么"
# （名字 + 一句描述 + 取正文的入口），并限定作用域在本会话，不写运行时承诺。
SKILL_ROSTER_NOTICE_HEADING = 'Skills newly readable in this session:'
SKILL_ROSTER_NOTICE_LOAD_HINT = 'Use `load_skill_context(skill_id="...")` to read a skill body.'
# 压缩跳带过去的那一段：抬头写明它没被压缩移出，模型不必再猜名单是否还有效。
SKILL_ROSTER_CARRIED_HEADING = '### Skills newly readable in this session (still readable; kept across compaction)'

SKILL_ROSTER_NOTICE_CONTEXT_KEY = 'skill_roster_notice_context'

_MAX_RECORDS = 64

# 进程内台账：会话道按 `session:<session_key>`、节点道按 `node:<task_id>:<node_id>` 分桶。
# 只有一处装配点读写它（旧机制的失效原因就是一条回合内四个读者共用一次消耗式读）。
# 与连续性侧车 / 节点 metadata 双向同步：重启与 pause-resume 后不重复投，
# 侧车写失败的最坏后果是下次重启重复投一条同样内容的提示，不是丢提示。
_STORE_LIMIT = 256
_CONTEXT_STORES: 'OrderedDict[str, dict[str, Any]]' = OrderedDict()


def clear_skill_roster_notice_stores() -> None:
    """测试隔离：进程内台账跨用例可见。"""
    _CONTEXT_STORES.clear()


def notice_context_for(scope_key: str) -> dict[str, Any]:
    return normalize_skill_roster_notice_context(_CONTEXT_STORES.get(str(scope_key or '').strip()))


def set_notice_context(scope_key: str, context: Any) -> dict[str, Any]:
    normalized = normalize_skill_roster_notice_context(context)
    key = str(scope_key or '').strip()
    if not key:
        return normalized
    _CONTEXT_STORES[key] = normalized
    _CONTEXT_STORES.move_to_end(key)
    while len(_CONTEXT_STORES) > _STORE_LIMIT:
        _CONTEXT_STORES.popitem(last=False)
    return normalized


def drop_notice_scope(scope_key: str) -> None:
    _CONTEXT_STORES.pop(str(scope_key or '').strip(), None)


def deliver_for_scope(
    scope_key: str,
    live_skills: list[Any],
    *,
    now: str,
    covered_skill_ids: list[Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """每跳唯一动作：记账 + 有未宣布的就投一条。

    返回（消息或 None，本次宣布的条目）；None 表示本轮不追加提示。调用方拿条目去发
    前端事件，两条信息同源，不再回头扫文本。
    """
    context, _fresh = record_live_skills(
        notice_context_for(scope_key),
        live_skills,
        now=now,
        covered_skill_ids=covered_skill_ids,
    )
    pending = pending_notice_records(context)
    if not pending:
        set_notice_context(scope_key, context)
        return None, []
    message = build_notice_message(pending)
    if message is not None:
        context = mark_records_delivered(
            context,
            [str(record.get('skill_id') or '') for record in pending],
            now=now,
        )
    set_notice_context(scope_key, context)
    return message, (pending if message is not None else [])


def carry_for_scope(
    scope_key: str,
    *,
    compression_stage_id: str,
    readable_skill_ids: list[str],
    now: str,
) -> dict[str, Any] | None:
    """压缩跳唯一动作：搬运仍可读的名字（同一次压缩只一份），不可读的退场。"""
    context, carried = carry_records_for_compression(
        notice_context_for(scope_key),
        compression_stage_id=compression_stage_id,
        readable_skill_ids=readable_skill_ids,
        now=now,
    )
    set_notice_context(scope_key, context)
    return build_carried_message(carried)


def _now_iso(now: str) -> str:
    return str(now or '').strip()


def _record_value(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name) or '').strip()
    return str(getattr(item, name, '') or '').strip()


def normalize_notice_record(item: Any) -> dict[str, str] | None:
    skill_id = _record_value(item, 'skill_id')
    if not skill_id:
        return None
    return {
        'skill_id': skill_id,
        'display_name': _record_value(item, 'display_name') or skill_id,
        'description': _record_value(item, 'description'),
        'recorded_at': _record_value(item, 'recorded_at'),
        'delivered_at': _record_value(item, 'delivered_at'),
        'superseded_at': _record_value(item, 'superseded_at'),
    }


def normalize_skill_roster_notice_context(payload: Any) -> dict[str, Any]:
    """台账归一：按 skill_id 去重（后写的覆盖先写的），并丢掉退场条目以外的畸形行。"""
    source = payload if isinstance(payload, dict) else {}
    records: dict[str, dict[str, str]] = {}
    for item in list(source.get('records') or []):
        record = normalize_notice_record(item)
        if record is None:
            continue
        records[record['skill_id']] = record
    ordered = list(records.values())
    if len(ordered) > _MAX_RECORDS:
        ordered = ordered[-_MAX_RECORDS:]
    carried = [
        str(item or '').strip()
        for item in list(source.get('carried_stages') or [])
        if str(item or '').strip()
    ]
    return {
        'records': ordered,
        'carried_stages': sorted(set(carried)),
    }


def readable_skill_items(records: list[Any]) -> list[Any]:
    """只留"真能 load 出正文"的那些。

    `available=False` 属 repair-required（如 `missing required bins`）：头部钉住名单本来就不带
    它，它的出口是 `load_skill_context` 的 repair 载荷。宣布成"新增可读"会让模型去 load 一个
    修不好的技能。这类名字**不进台账**，所以之后被修好、真的可读时还是会宣布一次。
    """
    readable: list[Any] = []
    for item in list(records or []):
        if isinstance(item, dict):
            available = item.get('available', True)
        else:
            available = getattr(item, 'available', True)
        if available is False or str(available).strip().lower() in {'false', '0'}:
            continue
        readable.append(item)
    return readable


def record_live_skills(
    context: Any,
    live_skills: list[Any],
    *,
    now: str,
    covered_skill_ids: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把 live 可见集记进台账；返回（新台账，本轮需要新宣布的条目）。

    `covered_skill_ids` 是**头部钉住块那份名单**：模型每跳都在 system 首条里读到它，
    所以这些名字算"已宣布"，不再投提示——否则会话/节点的第一跳会把整份 60 个名字的
    名单重播一遍。头部在重印边界吸收了新名字之后，对应台账条目也被摘掉（名单以头部
    为权威，块不无限涨）。

    已退场（`superseded_at`）的名字**不再复活**：那条名字已经在某个已付费边界被判过
    "本会话读不到"，重新授权它属于配置动作，走头部重印那条道去通知。
    """
    normalized = normalize_skill_roster_notice_context(context)
    covered = {
        str(item or '').strip()
        for item in list(covered_skill_ids or [])
        if str(item or '').strip()
    }
    kept = [record for record in normalized['records'] if record['skill_id'] not in covered]
    known = {record['skill_id'] for record in kept}
    fresh: list[dict[str, str]] = []
    for item in list(live_skills or []):
        roster_item = normalize_skill_roster_item(item)
        if roster_item is None:
            continue
        skill_id = roster_item['skill_id']
        if skill_id in covered or skill_id in known:
            continue
        known.add(skill_id)
        fresh.append(
            {
                **roster_item,
                'recorded_at': _now_iso(now),
                'delivered_at': '',
                'superseded_at': '',
            }
        )
    normalized['records'] = [*kept, *fresh]
    return normalized, fresh


def pending_notice_records(context: Any) -> list[dict[str, str]]:
    """还没投过的条目（`delivered_at` 为空且未退场）。"""
    return [
        dict(record)
        for record in list(normalize_skill_roster_notice_context(context).get('records') or [])
        if not str(record.get('delivered_at') or '').strip()
        and not str(record.get('superseded_at') or '').strip()
    ]


def mark_records_delivered(context: Any, skill_ids: list[str], *, now: str) -> dict[str, Any]:
    delivered_ids = {str(item or '').strip() for item in list(skill_ids or []) if str(item or '').strip()}
    normalized = normalize_skill_roster_notice_context(context)
    stamp = _now_iso(now)
    for record in normalized['records']:
        if record['skill_id'] in delivered_ids and not record['delivered_at']:
            record['delivered_at'] = stamp
    return normalized


def render_notice_lines(records: list[Any], *, heading: str) -> list[str]:
    """名单本体两车道共用：`id | 名称 | 一句话描述`，按 id 排序去重。"""
    seen: set[str] = set()
    rows: list[tuple[str, str, str]] = []
    for item in list(records or []):
        record = normalize_notice_record(item)
        if record is None or record['skill_id'] in seen:
            continue
        seen.add(record['skill_id'])
        rows.append((record['skill_id'], record['display_name'], record['description']))
    if not rows:
        return []
    lines = [heading]
    for skill_id, display_name, description in sorted(rows):
        detail = description or 'No description available.'
        lines.append(f'- `{skill_id}` | {display_name} | {detail}')
    return lines
def render_notice_text(records: list[Any]) -> str:
    lines = [*render_notice_lines(records, heading=SKILL_ROSTER_NOTICE_HEADING), SKILL_ROSTER_NOTICE_LOAD_HINT]
    return '\n'.join([SKILL_ROSTER_NOTICE_MARKER, *lines]).strip()


def render_carried_text(records: list[Any]) -> str:
    return '\n'.join(
        [
            SKILL_ROSTER_NOTICE_MARKER,
            *render_notice_lines(records, heading=SKILL_ROSTER_CARRIED_HEADING),
        ]
    ).strip()


def build_notice_message(records: list[Any]) -> dict[str, Any] | None:
    """一次性投递那条消息：role=user，落在本轮历史末尾、两份运行时块之前。"""
    text = render_notice_text(records)
    if len(text.splitlines()) <= 2:
        return None
    return {'role': SKILL_ROSTER_NOTICE_ROLE, 'content': text}


def build_carried_message(records: list[Any]) -> dict[str, Any] | None:
    text = render_carried_text(records)
    if len(text.splitlines()) <= 2:
        return None
    return {'role': SKILL_ROSTER_NOTICE_ROLE, 'content': text}


def carry_records_for_compression(
    context: Any,
    *,
    compression_stage_id: str,
    readable_skill_ids: list[str],
    now: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """压缩跳的搬运与退场。

    - 已投递、尚未退场的条目：这次仍可读的带过去，不可读的写 `superseded_at` 后退场
      （不写"已下架"行——它的最终裁决归 `load_skill_context` 三态回执）。
    - 幂等按 `carried_stages`：同一次压缩只搬一份，连发多跳不叠第二份。
    """
    normalized = normalize_skill_roster_notice_context(context)
    stage_id = str(compression_stage_id or '').strip()
    readable = {str(item or '').strip() for item in list(readable_skill_ids or []) if str(item or '').strip()}
    stamp = _now_iso(now)
    carried: list[dict[str, str]] = []
    for record in normalized['records']:
        if record['superseded_at'] or not record['delivered_at']:
            continue
        if record['skill_id'] not in readable:
            record['superseded_at'] = stamp
            continue
        carried.append(dict(record))
    if not stage_id or stage_id in set(normalized['carried_stages']):
        return normalized, []
    if carried:
        normalized['carried_stages'] = [*normalized['carried_stages'], stage_id]
    return normalized, carried


def is_skill_roster_notice_text(text: Any) -> bool:
    normalized = str(text or '').strip()
    return normalized.startswith(SKILL_ROSTER_NOTICE_MARKER)


def strip_skill_roster_notice_echo(text: Any) -> str:
    """模型复读整段提示时只剥掉提示本身，正文前面的回答留着。

    与运行时块的剥离同一判据源：抬头标记固定，不从名单内容反推。
    """
    normalized = str(text or '')
    marker_index = normalized.find(SKILL_ROSTER_NOTICE_MARKER)
    if marker_index < 0:
        return normalized.strip()
    tail = normalized[marker_index:]
    if not (SKILL_ROSTER_NOTICE_HEADING in tail or SKILL_ROSTER_CARRIED_HEADING in tail):
        return normalized.strip()
    return normalized[:marker_index].strip()


def drop_records_after(context: Any, *, cutoff_iso: str) -> dict[str, Any]:
    """截断/Fork 撤回：`recorded_at` 不早于截断点的记录整条撤掉。

    与历史里的消息同源撤回——只撤历史不撤台账，那个名字就永久消失。
    """
    normalized = normalize_skill_roster_notice_context(context)
    cutoff = str(cutoff_iso or '').strip()
    if not cutoff:
        return {'records': [], 'carried_stages': []}
    kept = [
        dict(record)
        for record in normalized['records']
        if str(record.get('recorded_at') or '').strip() and str(record['recorded_at']) < cutoff
    ]
    return {'records': kept, 'carried_stages': normalized['carried_stages'] if kept else []}


__all__ = [
    'SKILL_ROSTER_CARRIED_HEADING',
    'SKILL_ROSTER_NOTICE_CONTEXT_KEY',
    'SKILL_ROSTER_NOTICE_HEADING',
    'SKILL_ROSTER_NOTICE_LOAD_HINT',
    'SKILL_ROSTER_NOTICE_MARKER',
    'SKILL_ROSTER_NOTICE_ROLE',
    'build_carried_message',
    'build_notice_message',
    'carry_for_scope',
    'carry_records_for_compression',
    'clear_skill_roster_notice_stores',
    'deliver_for_scope',
    'drop_notice_scope',
    'drop_records_after',
    'is_skill_roster_notice_text',
    'mark_records_delivered',
    'notice_context_for',
    'normalize_notice_record',
    'normalize_skill_roster_notice_context',
    'pending_notice_records',
    'readable_skill_items',
    'record_live_skills',
    'render_carried_text',
    'render_notice_lines',
    'render_notice_text',
    'set_notice_context',
    'strip_skill_roster_notice_echo',
]
