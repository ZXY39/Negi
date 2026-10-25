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

import hashlib
from collections import OrderedDict
from typing import Any

from g3ku.runtime.frontdoor.skill_roster import normalize_skill_roster_item

RUNTIME_NOTICE_MARKER = '[RUNTIME_NOTICE]'
SKILL_ROSTER_NOTICE_MARKER = RUNTIME_NOTICE_MARKER
# 存量转录里的通知仍是带版本号的旧拼写：读侧都认，写侧只出新拼写。
SKILL_ROSTER_NOTICE_MARKER_ALIASES = ('[SKILL_ROSTER_NOTICE_V1]',)
SKILL_ROSTER_NOTICE_MARKERS = (RUNTIME_NOTICE_MARKER, *SKILL_ROSTER_NOTICE_MARKER_ALIASES)
SKILL_ROSTER_NOTICE_ROLE = 'user'
# 模型可见文案一律英文：这条提示会长期留在可复用前缀里，措辞只写"该做什么"
# （名字 + 一句描述 + 取正文的入口），并限定作用域在本会话，不写运行时承诺。
SKILL_ROSTER_NOTICE_HEADING = 'Skills newly readable in this session:'
SKILL_ROSTER_NOTICE_LOAD_HINT = 'Use `load_skill_context(skill_id="...")` to read a skill body.'
# 压缩跳带过去的那一段：抬头写明它没被压缩移出，模型不必再猜名单是否还有效。
SKILL_ROSTER_CARRIED_HEADING = '### Skills newly readable in this session (still readable; kept across compaction)'
# 长期记忆增量与 skill 名单共用一条通知消息；两节之间用空行分隔。
MEMORY_NOTICE_HEADING = '### Long-term memory updates'
# 头部快照与当刻值的差量：两次重印之间唯一的变化出口。
RUNTIME_CONTRACT_UPDATE_HEADING = '### Runtime contract updates (supersede the pinned snapshot)'
# 渲染顺序固定：同一组变化每次投出来的字节稳定，便于模型与测试比对。
CONTRACT_SECTION_ORDER = (
    'exec_runtime_policy',
    'session_temp_dir',
    'denied_tools',
    'candidate_tools',
    'repair_required',
    'attachment_targets',
)

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
    memory_provider: Any = None,
    persona_provider: Any = None,
    contract_sections: dict[str, Any] | None = None,
    covered_contract_sections: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """每跳唯一动作：记账 + 有未宣布的就投一条。

    返回（消息或 None，本次宣布的 skill 条目）；None 表示本轮不追加提示。调用方拿条目
    去发前端事件，两条信息同源，不再回头扫文本。

    `memory_provider(since_rev=..., base_rev=...)` 返回 `{rev, text, rebase}`：记忆增量
    与 skill 名单合成同一条消息，游标随消息一起推进（同一次投递、同一份耐久基线）。

    `contract_sections` 是运行时合同的当刻值（key → 单行正文），`covered_contract_sections`
    是头部快照里已经覆盖的那几组（初值用它，避免把头部已有的值再宣布一遍）。两组只在值
    变化时进通知，台账按摘要去重。
    """
    context, _fresh = record_live_skills(
        notice_context_for(scope_key),
        live_skills,
        now=now,
        covered_skill_ids=covered_skill_ids,
    )
    pending = pending_notice_records(context)
    cursor = dict(context.get('memory_cursor') or {})
    memory_text = ''
    if callable(memory_provider):
        try:
            delta = memory_provider(
                since_rev=int(cursor.get('rev') or 0),
                base_rev=int(cursor.get('base_rev') or 0),
                initialized=bool(cursor.get('initialized')),
            )
        except Exception:
            delta = None
        if isinstance(delta, dict):
            next_rev = max(int(delta.get('rev') or 0), 0)
            text = str(delta.get('text') or '').strip()
            if text:
                memory_text = text
            next_cursor = {
                'rev': max(next_rev, int(cursor.get('rev') or 0)),
                'base_rev': int(cursor.get('base_rev') or 0),
                'initialized': True,
            }
            if delta.get('rebase') or delta.get('align'):
                next_cursor['base_rev'] = next_rev
            cursor = next_cursor
    context['memory_cursor'] = cursor
    persona_text = ''
    persona_cursor = dict(context.get('persona_cursor') or {})
    if callable(persona_provider):
        try:
            persona_delta = persona_provider(
                cursor_digest=str(persona_cursor.get('digest') or ''),
                initialized=bool(persona_cursor.get('initialized')),
            )
        except Exception:
            persona_delta = None
        if isinstance(persona_delta, dict):
            next_digest = str(persona_delta.get('digest') or '').strip()
            next_text = str(persona_delta.get('text') or '').strip()
            if next_text:
                persona_text = next_text
            persona_cursor = {'digest': next_digest, 'initialized': True}
    context['persona_cursor'] = persona_cursor
    context, changed_contract = record_contract_sections(
        context,
        contract_sections or {},
        now=now,
        covered_sections=covered_contract_sections,
    )
    if not pending and not memory_text and not persona_text and not changed_contract:
        set_notice_context(scope_key, context)
        return None, []
    message = build_notice_message(
        pending,
        memory_text=memory_text,
        persona_text=persona_text,
        contract_sections=changed_contract,
    )
    if message is not None:
        if pending:
            context = mark_records_delivered(
                context,
                [str(record.get('skill_id') or '') for record in pending],
                now=now,
            )
        if changed_contract:
            context = mark_contract_sections_delivered(
                context,
                [key for key, _text in changed_contract],
                now=now,
            )
        context['memory_cursor'] = cursor
    set_notice_context(scope_key, context)
    return message, (pending if message is not None else [])


def session_scope_key(session_key: Any) -> str:
    """前门台账作用域：按会话一条，与节点道 `node:<task>:<node>` 同构。"""
    normalized = str(session_key or '').strip()
    return f'session:{normalized}' if normalized else ''


def reset_memory_cursor(scope_key: str) -> None:
    """压缩跳的唯一动作：下一跳重新以整份快照作基座。

    提示正文被压缩重写后，之前的增量行已经不在上下文里；游标停在原处会让那些记忆
    永久不再出现，所以在这里归零，由下一次投递走 `rebase`。
    """
    context = notice_context_for(scope_key)
    context['memory_cursor'] = {'rev': 0, 'base_rev': 0, 'initialized': False}
    set_notice_context(scope_key, context)


def reset_persona_cursor(scope_key: str) -> None:
    """压缩跳：头部人设块已重印，下一跳以采纳值对齐，不重播同一条通知。"""
    context = notice_context_for(scope_key)
    context['persona_cursor'] = {'digest': '', 'initialized': False}
    set_notice_context(scope_key, context)


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


def _normalize_contract_text(value: Any) -> str:
    text = str(value or '').strip()
    # 单节正文只承载"当前值"，不是文档：超长截断避免把整张候选表塞进通知。
    if len(text) > 4000:
        text = text[:4000].rstrip() + '…'
    return text


def contract_section_digest(text: Any) -> str:
    return hashlib.sha1(_normalize_contract_text(text).encode('utf-8')).hexdigest()[:16]


def normalize_contract_sections(payload: Any) -> dict[str, dict[str, str]]:
    source = payload if isinstance(payload, dict) else {}
    normalized: dict[str, dict[str, str]] = {}
    for key, item in source.items():
        name = str(key or '').strip()
        if not name or not isinstance(item, dict):
            continue
        text = _normalize_contract_text(item.get('text'))
        normalized[name] = {
            'digest': str(item.get('digest') or contract_section_digest(text)).strip(),
            'text': text,
            'delivered_at': str(item.get('delivered_at') or '').strip(),
        }
    return normalized


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
    cursor_source = source.get('memory_cursor') if isinstance(source.get('memory_cursor'), dict) else {}
    persona_source = source.get('persona_cursor') if isinstance(source.get('persona_cursor'), dict) else {}

    def _cursor_value(name: str) -> int:
        try:
            return max(int(cursor_source.get(name) or 0), 0)
        except (TypeError, ValueError):
            return 0

    return {
        'records': ordered,
        'carried_stages': sorted(set(carried)),
        'memory_cursor': {
            'rev': _cursor_value('rev'),
            'base_rev': _cursor_value('base_rev'),
            'initialized': bool(cursor_source.get('initialized')),
        },
        'persona_cursor': {
            'digest': str(persona_source.get('digest') or '')[:64],
            'initialized': bool(persona_source.get('initialized')),
        },
        'contract_sections': normalize_contract_sections(source.get('contract_sections')),
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


def _ordered_contract_keys(keys: Any) -> list[str]:
    seen = {str(key or '').strip() for key in list(keys or [])}
    ordered = [key for key in CONTRACT_SECTION_ORDER if key in seen]
    ordered.extend(sorted(key for key in seen if key and key not in CONTRACT_SECTION_ORDER))
    return ordered


def record_contract_sections(
    context: Any,
    sections: dict[str, Any],
    *,
    now: str,
    covered_sections: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """把运行时合同当刻值记进台账；返回（新台账，本次需要投递的变化）。

    - 头部快照覆盖的组（`covered_sections`）在台账缺条目时以头部原文为初值，标记已投；
    - 其它组缺条目时：当刻为空视为"无需宣布"，当刻非空则投一条状态快照；
    - 已有条目按摘要比较，变化才投；变空也投（正文渲染 `none`，告诉模型已撤销）。
    """
    normalized = normalize_skill_roster_notice_context(context)
    table = dict(normalized['contract_sections'])
    covered = {
        str(key or '').strip(): _normalize_contract_text(value)
        for key, value in dict(covered_sections or {}).items()
        if str(key or '').strip()
    }
    normalized_current = {
        str(key or '').strip(): _normalize_contract_text(value)
        for key, value in dict(sections or {}).items()
        if str(key or '').strip()
    }
    changed: list[tuple[str, str]] = []
    for key in _ordered_contract_keys({*covered.keys(), *normalized_current.keys()}):
        current_text = normalized_current.get(key, '')
        entry = table.get(key)
        if entry is None:
            if key in covered:
                table[key] = {
                    'digest': contract_section_digest(covered[key]),
                    'text': covered[key],
                    'delivered_at': _now_iso(now),
                }
                continue
            if not current_text:
                table[key] = {
                    'digest': contract_section_digest(''),
                    'text': '',
                    'delivered_at': _now_iso(now),
                }
                continue
            changed.append((key, current_text))
            table[key] = {
                'digest': contract_section_digest(current_text),
                'text': current_text,
                'delivered_at': '',
            }
            continue
        if contract_section_digest(current_text) != entry['digest']:
            changed.append((key, current_text))
            table[key] = {
                'digest': contract_section_digest(current_text),
                'text': current_text,
                'delivered_at': '',
            }
    normalized['contract_sections'] = table
    return normalized, changed


def pending_contract_sections(context: Any) -> list[tuple[str, str]]:
    normalized = normalize_skill_roster_notice_context(context)
    pending: list[tuple[str, str]] = []
    for key in _ordered_contract_keys(normalized['contract_sections'].keys()):
        entry = normalized['contract_sections'][key]
        if str(entry.get('delivered_at') or '').strip():
            continue
        pending.append((key, str(entry.get('text') or '')))
    return pending


def mark_contract_sections_delivered(context: Any, keys: list[str], *, now: str) -> dict[str, Any]:
    normalized = normalize_skill_roster_notice_context(context)
    stamp = _now_iso(now)
    wanted = {str(key or '').strip() for key in list(keys or []) if str(key or '').strip()}
    for key, entry in normalized['contract_sections'].items():
        if key in wanted and not str(entry.get('delivered_at') or '').strip():
            entry['delivered_at'] = stamp
    return normalized


def render_contract_update_lines(sections: list[tuple[str, str]] | None) -> list[str]:
    rows = [
        (str(key or '').strip(), _normalize_contract_text(text))
        for key, text in list(sections or [])
        if str(key or '').strip()
    ]
    if not rows:
        return []
    lines = [RUNTIME_CONTRACT_UPDATE_HEADING]
    for key, text in rows:
        lines.append(f'- {key}: {text or "none"}')
    return lines


def seed_contract_sections(
    scope_key: str,
    sections: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    """把一组值标记为"头部已覆盖"；压缩后由调用方在重印头部之后调用。"""
    context = notice_context_for(scope_key)
    context, _changed = record_contract_sections(
        context,
        {},
        now=now,
        covered_sections=sections,
    )
    set_notice_context(scope_key, context)
    return context


def reset_contract_sections_for_compaction(scope_key: str, *, now: str = '') -> dict[str, Any]:
    """压缩跳：清空合同台账与记忆游标。

    头部已用当刻值重印，下一跳的单生产者会：头部覆盖项直接以头部原文为初值（不重复
    宣布），非头部项（denied/candidate/repair/attachments）若非空则投一条状态快照。
    """
    context = notice_context_for(scope_key)
    context['contract_sections'] = {}
    context['memory_cursor'] = {'rev': 0, 'base_rev': 0, 'initialized': False}
    context['persona_cursor'] = {'digest': '', 'initialized': False}
    _ = now
    set_notice_context(scope_key, context)
    return context


def is_runtime_notice_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    if str(message.get('role') or '').strip() != SKILL_ROSTER_NOTICE_ROLE:
        return False
    content = str(message.get('content') or '').strip()
    return content.startswith(SKILL_ROSTER_NOTICE_MARKERS)


def strip_runtime_notice_messages(messages: list[Any] | None) -> list[dict[str, Any]]:
    """压缩产物重建时剥离旧通知：头部已刷新，旧增量不再需要留在正文里。"""
    return [
        dict(item)
        for item in list(messages or [])
        if isinstance(item, dict) and not is_runtime_notice_message(item)
    ]


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


def build_notice_message(
    records: list[Any],
    *,
    memory_text: str = '',
    persona_text: str = '',
    contract_sections: list[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """一次性投递那条消息：role=user，落在本轮历史末尾。

    各节共用一条消息：skill 名单在前，记忆增量居中，人设更新在其后，运行时合同变化在最后
    （最近处，便于模型把"以最新通知为准"落在下一次判断上）；空节省略。
    """
    sections: list[str] = []
    skill_text = render_notice_text(records) if records else ''
    if len(skill_text.splitlines()) > 2:
        sections.append(skill_text)
    memory_section = str(memory_text or '').strip()
    if memory_section:
        sections.append('\n'.join([MEMORY_NOTICE_HEADING, memory_section]))
    persona_section = str(persona_text or '').strip()
    if persona_section:
        sections.append(persona_section)
    contract_lines = render_contract_update_lines(contract_sections)
    if contract_lines:
        sections.append('\n'.join(contract_lines))
    if not sections:
        return None
    body = '\n\n'.join(sections).strip()
    # `render_notice_text` 自带标记；此前这里再拼一次，实盘通知变成
    # `[RUNTIME_NOTICE]` 连写两行（节点道用例一直是红的）。标记只保留一份。
    if not body.startswith(SKILL_ROSTER_NOTICE_MARKER):
        body = '\n\n'.join([SKILL_ROSTER_NOTICE_MARKER, body])
    return {'role': SKILL_ROSTER_NOTICE_ROLE, 'content': body}


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
    return normalized.startswith(SKILL_ROSTER_NOTICE_MARKERS)


def strip_skill_roster_notice_echo(text: Any) -> str:
    """模型复读整段提示时只剥掉提示本身，正文前面的回答留着。

    与运行时块的剥离同一判据源：抬头标记固定，不从名单内容反推。
    """
    normalized = str(text or '')
    marker_indexes = [normalized.find(marker) for marker in SKILL_ROSTER_NOTICE_MARKERS]
    marker_indexes = [index for index in marker_indexes if index >= 0]
    if not marker_indexes:
        return normalized.strip()
    marker_index = min(marker_indexes)
    tail = normalized[marker_index:]
    if not (SKILL_ROSTER_NOTICE_HEADING in tail or SKILL_ROSTER_CARRIED_HEADING in tail):
        return normalized.strip()
    return normalized[:marker_index].strip()


def drop_records_after(context: Any, *, cutoff_iso: str) -> dict[str, Any]:
    """截断/Fork 撤回：`recorded_at` 不早于截断点的记录整条撤掉。

    与历史里的消息同源撤回——只撤历史不撤台账，那个名字就永久消失。合同分节同理：
    `delivered_at` 不早于截断点的那几条一并撤掉，下一跳会重新投当前值。
    """
    normalized = normalize_skill_roster_notice_context(context)
    cutoff = str(cutoff_iso or '').strip()
    if not cutoff:
        return {'records': [], 'carried_stages': [], 'contract_sections': {}}
    kept = [
        dict(record)
        for record in normalized['records']
        if str(record.get('recorded_at') or '').strip() and str(record['recorded_at']) < cutoff
    ]
    kept_contract = {
        key: dict(entry)
        for key, entry in normalized['contract_sections'].items()
        if not str(entry.get('delivered_at') or '').strip()
        or str(entry.get('delivered_at') or '').strip() < cutoff
    }
    return {
        'records': kept,
        'carried_stages': normalized['carried_stages'] if kept else [],
        'contract_sections': kept_contract,
    }


__all__ = [
    'CONTRACT_SECTION_ORDER',
    'MEMORY_NOTICE_HEADING',
    'RUNTIME_CONTRACT_UPDATE_HEADING',
    'RUNTIME_NOTICE_MARKER',
    'SKILL_ROSTER_CARRIED_HEADING',
    'SKILL_ROSTER_NOTICE_CONTEXT_KEY',
    'SKILL_ROSTER_NOTICE_HEADING',
    'SKILL_ROSTER_NOTICE_LOAD_HINT',
    'SKILL_ROSTER_NOTICE_MARKER',
    'SKILL_ROSTER_NOTICE_MARKER_ALIASES',
    'SKILL_ROSTER_NOTICE_MARKERS',
    'SKILL_ROSTER_NOTICE_ROLE',
    'RUNTIME_NOTICE_MARKER',
    'build_carried_message',
    'build_notice_message',
    'carry_for_scope',
    'carry_records_for_compression',
    'clear_skill_roster_notice_stores',
    'contract_section_digest',
    'deliver_for_scope',
    'drop_notice_scope',
    'drop_records_after',
    'is_runtime_notice_message',
    'is_skill_roster_notice_text',
    'mark_contract_sections_delivered',
    'mark_records_delivered',
    'normalize_contract_sections',
    'normalize_notice_record',
    'normalize_skill_roster_notice_context',
    'notice_context_for',
    'pending_contract_sections',
    'pending_notice_records',
    'readable_skill_items',
    'record_contract_sections',
    'record_live_skills',
    'render_carried_text',
    'render_contract_update_lines',
    'render_notice_lines',
    'render_notice_text',
    'reset_contract_sections_for_compaction',
    'reset_memory_cursor',
    'reset_persona_cursor',
    'seed_contract_sections',
    'session_scope_key',
    'set_notice_context',
    'strip_runtime_notice_messages',
    'strip_skill_roster_notice_echo',
]
