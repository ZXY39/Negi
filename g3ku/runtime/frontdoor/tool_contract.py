from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND = 'frontdoor_runtime_tool_contract'
FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING = '## Runtime Tool Contract'
FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY = '_frontdoor_tool_contract_payload'
# 活状态块：`callable_tools` / `stage_summary` 每次阶段切换都重写
# （实盘 1001 跳里分别 301 与 362 次），其余段一回合内是常量。拆成两份后，重写只
# 顶掉这份小块的字节，稳定块与它前面的携带正文继续命中前缀缓存。
FRONTDOOR_DYNAMIC_STAGE_GATE_KIND = 'frontdoor_runtime_stage_gate'
FRONTDOOR_DYNAMIC_STAGE_GATE_HEADING = '## Runtime Stage Gate'
RUNTIME_APPENDIX_HEADINGS = (FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING, FRONTDOOR_DYNAMIC_STAGE_GATE_HEADING)

# 钉住的静态声明：技能名单、执行策略、会话临时目录进的是 `stable_messages[0]` 那段
# system_prompt 文本（`message_builder` 拼 `## Capability Exposure Snapshot` 的同一位置），
# 不是新消息项——因此尾部重铺那条路（`_with_dynamic_appendix_at_tail` 只剥"运行时块"）
# 根本碰不到它，也就不需要第三套消息分类。
# 刷新键 = 曝光 revision + 策略签名 + 临时目录：三者任一动了才重写头部，代价是那一次
# 整段重算；没动就逐字节复用，名单/策略/路径每跳 0 计费。头部因此永远是当刻真值，
# 不存在"过期副本 + 注记纠正"这层结构。
FRONTDOOR_PINNED_CONTRACT_HEADING = '## Runtime Contract (pinned)'
FRONTDOOR_PINNED_CONTRACT_KIND = 'frontdoor_runtime_pinned_contract'
FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR = '_frontdoor_pinned_contract_text'
FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR = '_frontdoor_pinned_contract_revision'
FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR = '_frontdoor_pinned_contract_skill_ids'


def _render_name_list_for_pinned(items: list[Any] | None) -> str:
    return _render_name_list(_normalized_name_list(items))


def _exec_policy_signature(exec_runtime_policy: dict[str, Any] | None) -> str:
    payload = dict(exec_runtime_policy or {})
    if not payload:
        return ''
    return '|'.join(
        str(payload.get(key) if payload.get(key) is not None else '')
        for key in ('mode', 'guardrails_enabled', 'summary')
    )


def pinned_contract_revision_key(
    *,
    skill_ids: list[Any] | None,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
    contract_revision: str | None,
) -> str:
    """头部块的重印判据：三项内容 + 曝光提交点，合成一个短键。"""
    rendered = render_pinned_contract_text(
        skill_ids=skill_ids,
        exec_runtime_policy=exec_runtime_policy,
        session_temp_dir=session_temp_dir,
    )
    digest = hashlib.sha256(f'{contract_revision or ""}\n{rendered}'.encode('utf-8')).hexdigest()[:16]
    return f'pc:{digest}'


def render_pinned_contract_text(
    *,
    skill_ids: list[Any] | None,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
) -> str:
    """渲染钉住块。三项全空时返回空串（装配侧据此不追加头部、尾块照旧）。"""
    lines = [
        FRONTDOOR_PINNED_CONTRACT_HEADING,
        f'kind: {FRONTDOOR_PINNED_CONTRACT_KIND}',
        f'candidate_skills (loadable with `load_skill_context`): {_render_name_list_for_pinned(skill_ids)}',
    ]
    policy_line = _render_exec_runtime_policy(exec_runtime_policy)
    if policy_line:
        lines.append(policy_line)
    temp_line = _render_session_temp_dir(session_temp_dir)
    if temp_line:
        lines.extend(temp_line)
    return '\n'.join(lines).strip()


def frontdoor_pinned_contract_text(
    session: Any,
    *,
    skill_ids: list[Any] | None,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
    contract_revision: str | None,
) -> str:
    """注入侧唯一取数口：命中已钉住的内容就复用原文，否则重钉一次并记下判据键。

    读一次写一次都在同一轮内完成，重复调用幂等（同样的输入渲染出同样的串）。会话不可用
    （None / 无属性写入口）时退化为"本轮现算，不钉"，尾块继续带这三段——宁可重复不可缺。
    """
    if session is None:
        return ''
    revision = pinned_contract_revision_key(
        skill_ids=skill_ids,
        exec_runtime_policy=exec_runtime_policy,
        session_temp_dir=session_temp_dir,
        contract_revision=contract_revision,
    )
    stored = getattr(session, FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR, None)
    stored_revision = str(getattr(session, FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR, '') or '').strip()
    if isinstance(stored, str) and stored and stored_revision == revision:
        return stored
    fresh = render_pinned_contract_text(
        skill_ids=skill_ids,
        exec_runtime_policy=exec_runtime_policy,
        session_temp_dir=session_temp_dir,
    )
    if not fresh:
        return ''
    try:
        setattr(session, FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR, fresh)
        setattr(session, FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR, revision)
        setattr(
            session,
            FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR,
            _normalized_name_list(skill_ids),
        )
    except Exception:
        return ''
    return fresh


def pinned_skill_ids_for(session: Any) -> list[str]:
    """钉住块里那份名单（差集算式的左操作数）。"""
    return _normalized_name_list(getattr(session, FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR, None) or [])


def pinned_skill_difference(
    *,
    pinned_skill_ids: list[Any] | None,
    round_skill_ids: list[Any] | None,
) -> tuple[list[str], list[str]]:
    """成员差，不是数量差：本轮选中但头部没声明的 / 头部声明了但本轮不再选中的。"""
    pinned = _normalized_name_list(pinned_skill_ids)
    current = _normalized_name_list(round_skill_ids)
    granted = [name for name in current if name not in set(pinned)]
    unselected = [name for name in pinned if name not in set(current)]
    return granted, unselected



def _normalized_name_list(items: list[Any] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        normalized = str(item or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _normalized_candidate_tool_items(
    items: list[Any] | None,
    *,
    fallback_names: list[str] | None = None,
) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_items = list(items or [])
    if not raw_items and fallback_names:
        raw_items = list(fallback_names)
    for item in raw_items:
        if isinstance(item, dict):
            tool_id = str(item.get('tool_id') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            tool_id = str(item or '').strip()
            description = ''
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append({'tool_id': tool_id, 'description': description})
    return ordered


def _normalized_repair_required_tool_items(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get('tool_id') or '').strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append(
            {
                'tool_id': tool_id,
                'description': str(item.get('description') or '').strip(),
                'reason': str(item.get('reason') or '').strip(),
            }
        )
    return ordered


def _normalized_repair_required_skill_items(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get('skill_id') or '').strip()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        ordered.append(
            {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
                'reason': str(item.get('reason') or '').strip(),
            }
        )
    return ordered


def normalize_frontdoor_candidate_tool_items(
    items: list[Any] | None,
    *,
    fallback_names: list[str] | None = None,
) -> list[dict[str, str]]:
    return _normalized_candidate_tool_items(items, fallback_names=fallback_names)


def _normalized_attachment_reopen_targets(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        path = str(item.get('path') or '').strip()
        ref = str(item.get('ref') or '').strip()
        if not path and not ref:
            continue
        dedupe_key = ref or path
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entry = {
            'name': str(item.get('name') or path or ref).strip() or (path or ref),
            'kind': str(item.get('kind') or '').strip(),
            'mime_type': str(item.get('mime_type') or item.get('mimeType') or '').strip(),
            'path': path,
            'ref': ref,
        }
        ordered.append({key: value for key, value in entry.items() if value})
    return ordered


def _render_name_list(items: list[str] | None) -> str:
    names = _normalized_name_list(items)
    if not names:
        return 'none'
    return ', '.join(f'`{name}`' for name in names)


def _render_candidate_tool_section(items: list[dict[str, str]] | None) -> list[str]:
    """只列名字：每个工具的说明文字由 provider `tools[]` 的 `function.description` 承载
    （见 `_provider_visible_tool_contract`），这里不再抄第二份。
    """
    normalized_items = _normalized_candidate_tool_items(items)
    if not normalized_items:
        return ['candidate_tools: none']
    return [f'candidate_tools: {_render_name_list([str(item.get("tool_id") or "") for item in normalized_items])}']


def _render_repair_required_tool_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_repair_required_tool_items(items)
    if not normalized_items:
        return []
    lines = [
        'repair_required_tools:',
        '- These tools must be repaired before use.',
        '- Use `load_tool_context(tool_id="<tool_id>")` first.',
        '- Use `exec`, `filesystem_write`, `filesystem_edit`, `filesystem_copy`, `filesystem_move`, or `filesystem_propose_patch` to repair them.',
        '- Reference skill: `repair-tool`.',
    ]
    for item in normalized_items:
        tool_id = str(item.get('tool_id') or '').strip()
        description = str(item.get('description') or '').strip()
        reason = str(item.get('reason') or '').strip()
        detail = description if description else 'No description available.'
        if reason:
            detail = f'{detail} Reason: {reason}'
        lines.append(f'- `{tool_id}`: {detail}')
    return lines


def _render_repair_required_skill_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_repair_required_skill_items(items)
    if not normalized_items:
        return []
    lines = [
        'repair_required_skills:',
        '- These skills must be repaired before viewing their body.',
        '- Do not call `load_skill_context` until repaired.',
        '- Use `exec`, `filesystem_write`, `filesystem_edit`, `filesystem_copy`, `filesystem_move`, or `filesystem_propose_patch` to repair them.',
        '- Reference skill: `writing-skills`.',
    ]
    for item in normalized_items:
        skill_id = str(item.get('skill_id') or '').strip()
        description = str(item.get('description') or '').strip()
        reason = str(item.get('reason') or '').strip()
        detail = description if description else 'No description available.'
        if reason:
            detail = f'{detail} Reason: {reason}'
        lines.append(f'- `{skill_id}`: {detail}')
    return lines


def _render_attachment_reopen_target_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_attachment_reopen_targets(items)
    if not normalized_items:
        return []
    lines = [
        'attachment_reopen_targets:',
        '- These uploaded files remain reopenable in later turns.',
        '- If a detached task must read one of them, copy the exact `path:` or `ref:` into `create_async_task.file_targets`.',
        '- `create_async_task.file_targets` is the authoritative reopen lane for detached tasks.',
        '- A bare filename like `resume.docx` is not a valid reopen target; use the exact absolute `path` or exact `ref`.',
        '- If you provide `path`, runtime rejects relative paths and paths that do not point to an existing file.',
        '- In `create_async_task.task`, describe why the file matters and how it should be used, but do not rely on prose alone for reopen handles.',
        '- Do not replace them with placeholders like `current_uploads`, `user_uploads`, or `user_image_and_docx`.',
    ]
    for item in normalized_items:
        name = str(item.get('name') or '').strip() or 'attachment'
        kind = str(item.get('kind') or '').strip() or 'file'
        mime_type = str(item.get('mime_type') or '').strip() or 'application/octet-stream'
        details = [f'kind={kind}', f'mime_type={mime_type}']
        path = str(item.get('path') or '').strip()
        ref = str(item.get('ref') or '').strip()
        if path:
            details.append(f'path={path}')
        if ref:
            details.append(f'ref={ref}')
        lines.append(f'- `{name}`: ' + '; '.join(details))
    return lines


def _render_stage_summary(stage_summary: dict[str, Any] | None) -> str:
    payload = dict(stage_summary or {})
    active_stage_id = str(payload.get('active_stage_id') or '').strip() or 'none'
    transition_required = bool(payload.get('transition_required'))
    active_stage = dict(payload.get('active_stage') or {}) if isinstance(payload.get('active_stage'), dict) else {}
    pending = [dict(item) for item in list(payload.get('pending_orphan_rounds') or []) if isinstance(item, dict)]
    pending_counted = sum(1 for round_item in pending if bool(round_item.get('budget_counted')))
    if not active_stage:
        guard = (
            'current stage budget is exhausted; submit `submit_next_stage` together with the tools '
            'you want to call in the next round, otherwise the call will be blocked'
            if transition_required
            else 'no active stage — normal at the start of every turn; when you need tools, submit '
            '`submit_next_stage` together with them in the same batch (submit_next_stage runs first, '
            'then the tools are booked on the first round of the new stage); calling ordinary tools '
            'alone gets one grace execution, then is blocked'
        )
        rendered = (
            f'stage_summary: active_stage_id={active_stage_id}; '
            f'transition_required={transition_required}; {guard}'
        )
        if pending:
            rendered += (
                f' {len(pending)} grace round(s) are still unbooked (of which {pending_counted} will consume budget); '
                'they will be merged into the new stage you open with the next `submit_next_stage`: cover them in '
                f'stage_goal / completed_stage_summary and set tool_round_budget to at least {len(pending)} + the rounds you still need.'
            )
        return rendered
    parts = [
        f'active_stage_id={active_stage_id}',
        f'transition_required={transition_required}',
    ]
    stage_goal = str(active_stage.get('stage_goal') or '').strip()
    if stage_goal:
        parts.append(f'stage_goal={stage_goal}')
    tool_round_budget = active_stage.get('tool_round_budget')
    if tool_round_budget not in (None, ''):
        parts.append(f'tool_round_budget={int(tool_round_budget)}')
    stage_kind = str(active_stage.get('stage_kind') or '').strip()
    if stage_kind:
        parts.append(f'stage_kind={stage_kind}')
    if 'final_stage' in active_stage:
        parts.append(f'final_stage={bool(active_stage.get("final_stage"))}')
    return 'stage_summary: ' + '; '.join(parts)


def _render_exec_runtime_policy(exec_runtime_policy: dict[str, Any] | None) -> str:
    payload = dict(exec_runtime_policy or {})
    if not payload:
        return 'exec_runtime_policy: none'
    parts: list[str] = []
    mode = str(payload.get('mode') or '').strip()
    if mode:
        parts.append(f'mode={mode}')
    if 'guardrails_enabled' in payload:
        parts.append(f'guardrails_enabled={bool(payload.get("guardrails_enabled"))}')
    summary = str(payload.get('summary') or '').strip()
    if summary:
        parts.append(f'summary={summary}')
    return 'exec_runtime_policy: ' + ('; '.join(parts) if parts else 'none')


def _contract_revision_line(payload: dict[str, Any]) -> str:
    return f'contract_revision: {str(payload.get("contract_revision") or "").strip() or "none"}'


def _render_frontdoor_contract_summary(payload: dict[str, Any]) -> str:
    """回合内常量部分：候选集、待修复、附件句柄、临时目录、执行策略。

    省略判定是**逐段**的，不看"钉住块是否存在"这种整体开关：头部文本里出现了哪一段，尾块
    才省哪一段。整体开关会在装配点缺某项输入时（例如某条路径不传 `session_temp_dir`）把那条
    声明整段变没——省缓存省掉一条声明，比不省严重得多。缺的那段照旧在本块出现，宁可重复
    不可缺失。
    """
    candidate_tools = _normalized_candidate_tool_items(payload.get('candidate_tools'))
    repair_required_tools = _normalized_repair_required_tool_items(payload.get('repair_required_tools'))
    repair_required_skills = _normalized_repair_required_skill_items(payload.get('repair_required_skills'))
    attachment_reopen_targets = _normalized_attachment_reopen_targets(payload.get('attachment_reopen_targets'))
    pinned_text = str(payload.get('pinned_contract_text') or '').strip()
    pinned_roster = 'candidate_skills (loadable with' in pinned_text
    pinned_exec = 'exec_runtime_policy' in pinned_text
    pinned_temp = 'session_temp_dir:' in pinned_text
    lines = [
        FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING,
        f'kind: {FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND}',
        _contract_revision_line(payload),
    ]
    if pinned_roster:
        granted, unselected = pinned_skill_difference(
            pinned_skill_ids=payload.get('pinned_skill_ids'),
            round_skill_ids=payload.get('candidate_skill_ids'),
        )
        if granted:
            lines.append(
                'granted_skills (本轮新增可见、头部名单里还没有，可直接 `load_skill_context`): '
                f'{_render_name_list(granted)}'
            )
        if unselected:
            lines.append(
                f'unselected_skills (头部名单声明了但本轮没选进候选，本轮调不动): {_render_name_list(unselected)}'
            )
    else:
        lines.append(
            f'candidate_skills (loadable with `load_skill_context`): {_render_name_list(payload.get("candidate_skill_ids"))}'
        )
    lines.extend(
        [
            *_render_attachment_reopen_target_section(attachment_reopen_targets),
            *_render_candidate_tool_section(candidate_tools),
            *_render_repair_required_tool_section(repair_required_tools),
            *_render_repair_required_skill_section(repair_required_skills),
        ]
    )
    if not pinned_exec:
        lines.append(_render_exec_runtime_policy(payload.get('exec_runtime_policy')))
    if not pinned_temp:
        lines.extend(_render_session_temp_dir(payload.get('session_temp_dir')))
    return '\n'.join(lines)


def _render_frontdoor_stage_gate_summary(payload: dict[str, Any]) -> str:
    """每跳活的状态：本轮真可调用集、水合集、活动阶段轨道。排在请求体末位。"""
    lines = [
        FRONTDOOR_DYNAMIC_STAGE_GATE_HEADING,
        f'kind: {FRONTDOOR_DYNAMIC_STAGE_GATE_KIND}',
        _contract_revision_line(payload),
        f'callable_tools: {_render_name_list(payload.get("callable_tool_names"))}',
    ]
    denied_tool_names = _normalized_name_list(payload.get('denied_tool_names'))
    if denied_tool_names:
        lines.append(
            'denied_tools (`tools[]` 里带着参数表，但你当前角色无权限，调用一律被拒；'
            f'不要反复重试或改着名字试): {_render_name_list(denied_tool_names)}'
        )
    lines.append(_render_stage_summary(payload.get('stage_summary')))
    return '\n'.join(lines)


def _render_session_temp_dir(session_temp_dir: Any) -> list[str]:
    """只给路径；规则本体在基础提示词 `ceo_frontdoor.md`「临时文件与中间产物」那条，
    它反过来写"以 runtime tool contract 中 `session_temp_dir` 给出的绝对路径为准"。
    """
    text = str(session_temp_dir or '').strip()
    if not text:
        return []
    return [f'session_temp_dir: {text}']


def _active_stage_prompt_view(active_stage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(active_stage, dict):
        return None
    stage_id = str(active_stage.get('stage_id') or '').strip()
    if not stage_id:
        return None
    return {
        'stage_id': stage_id,
        'stage_goal': str(active_stage.get('stage_goal') or '').strip(),
        'tool_round_budget': max(0, int(active_stage.get('tool_round_budget') or 0)),
        'stage_kind': str(active_stage.get('stage_kind') or 'normal').strip() or 'normal',
        'final_stage': bool(active_stage.get('final_stage', False)),
    }


def _active_stage_summary(frontdoor_stage_state: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(frontdoor_stage_state or {})
    active_stage_id = str(payload.get('active_stage_id') or '').strip()
    stages = [dict(item) for item in list(payload.get('stages') or []) if isinstance(item, dict)]
    active_stage = next(
        (
            item
            for item in stages
            if str(item.get('stage_id') or '').strip() == active_stage_id
        ),
        None,
    )
    pending_orphan_rounds = [
        dict(item) for item in list(payload.get('pending_orphan_rounds') or []) if isinstance(item, dict)
    ]
    return {
        'active_stage_id': active_stage_id,
        'transition_required': bool(payload.get('transition_required')),
        'active_stage': _active_stage_prompt_view(active_stage),
        'pending_orphan_rounds': pending_orphan_rounds,
    }


@dataclass(slots=True)
class FrontdoorToolContract:
    callable_tool_names: list[str]
    candidate_tool_names: list[str]
    hydrated_tool_names: list[str]
    stage_summary: dict[str, Any]
    visible_skill_ids: list[str]
    candidate_skill_ids: list[str]
    rbac_visible_tool_names: list[str]
    rbac_visible_skill_ids: list[str]
    contract_revision: str
    candidate_tool_items: list[dict[str, str]] | None = None
    candidate_skill_items: list[dict[str, str]] | None = None
    repair_required_tool_items: list[dict[str, str]] | None = None
    repair_required_skill_items: list[dict[str, str]] | None = None
    exec_runtime_policy: dict[str, Any] | None = None
    attachment_reopen_targets: list[dict[str, str]] | None = None
    denied_tool_names: list[str] | None = None
    session_temp_dir: str | None = None
    pinned_contract_text: str | None = None
    pinned_skill_ids: list[str] | None = None

    def to_message_payload(self) -> dict[str, Any]:
        payload = {
            'message_type': FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
            'callable_tool_names': list(self.callable_tool_names),
            'denied_tool_names': _normalized_name_list(list(self.denied_tool_names or [])),
            'candidate_tools': _normalized_candidate_tool_items(
                list(self.candidate_tool_items or []),
                fallback_names=list(self.candidate_tool_names),
            ),
            'hydrated_tool_names': list(self.hydrated_tool_names),
            'candidate_skill_ids': list(self.candidate_skill_ids),
            'stage_summary': dict(self.stage_summary),
            'contract_revision': str(self.contract_revision or '').strip(),
            'exec_runtime_policy': (
                dict(self.exec_runtime_policy)
                if isinstance(self.exec_runtime_policy, dict)
                else None
            ),
            'session_temp_dir': str(self.session_temp_dir or '').strip() or None,
        }
        attachment_reopen_targets = _normalized_attachment_reopen_targets(self.attachment_reopen_targets)
        if attachment_reopen_targets:
            payload['attachment_reopen_targets'] = attachment_reopen_targets
        repair_required_tools = _normalized_repair_required_tool_items(self.repair_required_tool_items)
        if repair_required_tools:
            payload['repair_required_tools'] = repair_required_tools
        repair_required_skills = _normalized_repair_required_skill_items(self.repair_required_skill_items)
        if repair_required_skills:
            payload['repair_required_skills'] = repair_required_skills
        pinned_text = str(self.pinned_contract_text or '').strip()
        if pinned_text:
            payload['pinned_contract_text'] = pinned_text
            payload['pinned_skill_ids'] = _normalized_name_list(self.pinned_skill_ids)
        return payload

    def to_message(self) -> dict[str, Any]:
        payload = self.to_message_payload()
        # system 角色：该契约是运行时元数据，不是对话内容；用 assistant 会让模型把它
        # 当成"自己上一轮说过/发给用户的消息"，进而在事后复盘时改写时间线。
        return {
            'role': 'system',
            'content': _render_frontdoor_contract_summary(payload),
            FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY: payload,
        }

    def to_stage_gate_message(self) -> dict[str, Any]:
        """活状态块：与契约同源，但只含每跳重写的行，排在请求体真正末位。"""
        return {
            'role': 'system',
            'content': _render_frontdoor_stage_gate_summary(self.to_message_payload()),
        }


def _frontdoor_tool_contract_payload_from_content(content: Any) -> dict[str, Any] | None:
    payload: dict[str, Any] | None = None
    if isinstance(content, dict):
        payload = dict(content)
    elif isinstance(content, str):
        text = str(content or '').strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            payload = dict(parsed)
    if not isinstance(payload, dict):
        return None
    if str(payload.get('message_type') or '').strip() != FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND:
        return None
    return payload


def _frontdoor_message_declares_tool_calls(message: dict[str, Any] | None) -> bool:
    if not isinstance(message, dict):
        return False
    for tool_call in list(message.get('tool_calls') or []):
        if isinstance(tool_call, dict):
            return True
    function_call = message.get('function_call')
    if isinstance(function_call, dict) and str(function_call.get('name') or '').strip():
        return True
    return False


def frontdoor_tool_contract_payload_from_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    # 运行时注入的契约消息从不携带工具调用。带 tool_calls 的 assistant 消息
    # 是模型回合本身——即使其文本回显了契约抬头/契约 JSON——也不得判为契约
    # 消息，否则该回合会被整体剥离，留下孤儿工具结果。
    if _frontdoor_message_declares_tool_calls(message):
        return None
    payload = message.get(FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY)
    if isinstance(payload, dict):
        resolved = dict(payload)
        if str(resolved.get('message_type') or '').strip() == FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND:
            return resolved
    return _frontdoor_tool_contract_payload_from_content((message or {}).get('content'))


def build_frontdoor_tool_contract(
    *,
    callable_tool_names: list[str] | None,
    candidate_tool_names: list[str] | None,
    candidate_tool_items: list[dict[str, str]] | None = None,
    hydrated_tool_names: list[str] | None,
    frontdoor_stage_state: dict[str, Any] | None,
    visible_skill_ids: list[str] | None = None,
    candidate_skill_ids: list[str] | None = None,
    candidate_skill_items: list[dict[str, str]] | None = None,
    repair_required_tool_items: list[dict[str, str]] | None = None,
    repair_required_skill_items: list[dict[str, str]] | None = None,
    rbac_visible_tool_names: list[str] | None = None,
    rbac_visible_skill_ids: list[str] | None = None,
    contract_revision: str | None = None,
    exec_runtime_policy: dict[str, Any] | None = None,
    attachment_reopen_targets: list[dict[str, str]] | None = None,
    session_temp_dir: str | None = None,
    denied_tool_names: list[str] | None = None,
    pinned_contract_text: str | None = None,
    pinned_skill_ids: list[str] | None = None,
) -> FrontdoorToolContract:
    callable_names = _normalized_name_list(callable_tool_names)
    candidate_names = [
        name
        for name in _normalized_name_list(candidate_tool_names)
        if name not in set(callable_names)
    ]
    return FrontdoorToolContract(
        callable_tool_names=callable_names,
        candidate_tool_names=candidate_names,
        hydrated_tool_names=_normalized_name_list(hydrated_tool_names),
        stage_summary=_active_stage_summary(frontdoor_stage_state),
        visible_skill_ids=_normalized_name_list(visible_skill_ids),
        candidate_skill_ids=_normalized_name_list(candidate_skill_ids),
        rbac_visible_tool_names=_normalized_name_list(rbac_visible_tool_names),
        rbac_visible_skill_ids=_normalized_name_list(rbac_visible_skill_ids),
        contract_revision=str(contract_revision or '').strip(),
        candidate_tool_items=_normalized_candidate_tool_items(candidate_tool_items, fallback_names=candidate_names),
        candidate_skill_items=list(candidate_skill_items or []),
        repair_required_tool_items=_normalized_repair_required_tool_items(repair_required_tool_items),
        repair_required_skill_items=_normalized_repair_required_skill_items(repair_required_skill_items),
        exec_runtime_policy=dict(exec_runtime_policy) if isinstance(exec_runtime_policy, dict) else None,
        attachment_reopen_targets=_normalized_attachment_reopen_targets(attachment_reopen_targets),
        session_temp_dir=str(session_temp_dir or '').strip() or None,
        denied_tool_names=_normalized_name_list(denied_tool_names),
        pinned_contract_text=str(pinned_contract_text or '').strip() or None,
        pinned_skill_ids=_normalized_name_list(pinned_skill_ids),
    )


def _runtime_appendix_pair(heading: str) -> str:
    return {
        FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING: FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
        FRONTDOOR_DYNAMIC_STAGE_GATE_HEADING: FRONTDOOR_DYNAMIC_STAGE_GATE_KIND,
    }[heading]


def is_frontdoor_tool_contract_message(message: dict[str, Any]) -> bool:
    """Whether a message is one of the runtime-injected appendix blocks.

    Both the turn-stable contract and the live stage gate are runtime metadata,
    so every strip/persist/transcript path must treat them the same way.
    """
    if frontdoor_tool_contract_payload_from_message(message) is not None:
        return True
    if _frontdoor_message_declares_tool_calls(message):
        return False
    # 注入的契约消息经 sanitize 后只保留 role/content，payload 键被剥离，需靠抬头识别。
    # 契约现在以 system 角色注入；模型回显的契约残留仍是 assistant 文本，两类都识别。
    if str((message or {}).get('role') or '').strip().lower() not in {'assistant', 'system'}:
        return False
    content = str((message or {}).get('content') or '').strip()
    return any(content.startswith(heading) for heading in RUNTIME_APPENDIX_HEADINGS)


def _frontdoor_tool_contract_heading_index(text: str) -> int:
    """Return the start of a rendered appendix block embedded in ``text``.

    The heading alone is not enough to classify ordinary user prose that
    happens to mention the contract.  Requiring the canonical kind marker in
    the nearby suffix keeps this helper focused on the provider-facing blocks
    that the runtime injects.  The live stage gate counts too: it is the block
    sitting at the continuation point, so it is the one a model can echo.
    """
    normalized = str(text or '')
    best = -1
    for heading in RUNTIME_APPENDIX_HEADINGS:
        heading_index = normalized.find(heading)
        if heading_index < 0:
            continue
        suffix = normalized[heading_index : heading_index + 512]
        if f'kind: {_runtime_appendix_pair(heading)}' not in suffix:
            continue
        if best < 0 or heading_index < best:
            best = heading_index
    return best


def is_frontdoor_tool_contract_echo_text(text: Any) -> bool:
    """Whether model/channel text is a standalone injected appendix-block echo."""
    normalized = str(text or '').strip()
    if not normalized:
        return False
    if any(normalized.startswith(heading) for heading in RUNTIME_APPENDIX_HEADINGS):
        return _frontdoor_tool_contract_heading_index(normalized) == 0
    return is_frontdoor_tool_contract_message({'role': 'assistant', 'content': normalized})


def strip_frontdoor_tool_contract_echo(text: Any) -> str:
    """Remove a rendered runtime appendix-block echo from user-facing text.

    A standalone echo becomes empty.  If a model puts a visible answer before
    the echoed block, preserve that answer and remove only the internal suffix.
    JSON contract payloads are handled by the standalone classifier above.
    """
    normalized = str(text or '')
    if is_frontdoor_tool_contract_echo_text(normalized):
        return ''
    heading_index = _frontdoor_tool_contract_heading_index(normalized)
    if heading_index >= 0:
        return normalized[:heading_index].strip()
    return normalized.strip()


def upsert_frontdoor_tool_contract_message(
    messages: list[dict[str, Any]] | None,
    contract: FrontdoorToolContract,
) -> list[dict[str, Any]]:
    """携带历史里 0 份运行时块，尾部恰好一份契约 + 一份活状态块（稳定在前、活在后）。"""
    appendix = [contract.to_message(), contract.to_stage_gate_message()]
    updated: list[dict[str, Any]] = []
    inserted = False
    for message in list(messages or []):
        if is_frontdoor_tool_contract_message(message):
            if not inserted:
                updated.extend(appendix)
                inserted = True
            continue
        updated.append(dict(message))
    if not inserted:
        updated.extend(appendix)
    return updated
