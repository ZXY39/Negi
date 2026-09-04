from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND = 'frontdoor_runtime_tool_contract'
FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING = '## Runtime Tool Contract'
FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY = '_frontdoor_tool_contract_payload'


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
    normalized_items = _normalized_candidate_tool_items(items)
    if not normalized_items:
        return ['candidate_tools: none']
    lines = [
        'candidate_tools:',
        '- These tools are visible but not callable yet. To use one, call `load_tool_context(tool_id="<tool_id>")` first and wait for the next round before calling it directly.',
    ]
    for item in normalized_items:
        tool_id = str(item.get('tool_id') or '').strip()
        description = str(item.get('description') or '').strip()
        detail = description if description else 'No description available.'
        lines.append(f'- `{tool_id}`: {detail}')
    return lines


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


def _render_frontdoor_contract_summary(payload: dict[str, Any]) -> str:
    candidate_tools = _normalized_candidate_tool_items(payload.get('candidate_tools'))
    repair_required_tools = _normalized_repair_required_tool_items(payload.get('repair_required_tools'))
    repair_required_skills = _normalized_repair_required_skill_items(payload.get('repair_required_skills'))
    attachment_reopen_targets = _normalized_attachment_reopen_targets(payload.get('attachment_reopen_targets'))
    lines = [
        FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING,
        f'kind: {FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND}',
        f'contract_revision: {str(payload.get("contract_revision") or "").strip() or "none"}',
        f'callable_tools: {_render_name_list(payload.get("callable_tool_names"))}',
        f'hydrated_tools: {_render_name_list(payload.get("hydrated_tool_names"))}',
        f'candidate_skills (loadable with `load_skill_context`): {_render_name_list(payload.get("candidate_skill_ids"))}',
        'load_skill_context_help: Skills listed in `candidate_skills` do not hydrate. Call `load_skill_context(skill_id="<skill_id>")` to read the skill body when `load_skill_context` is callable; if only `submit_next_stage` is callable, start a stage first.',
        'load_tool_context_help: Any surfaced RBAC-visible tool may be loaded by exact `tool_id` for docs/help, including tools that are already callable or already hydrated.',
        'load_tool_context_repeat_guard: For callable, hydrated, or fixed-builtin tools, do not reread the same inline uncompressed toolskill. Reuse it unless the tool state changed or the old result was compressed away.',
        *_render_attachment_reopen_target_section(attachment_reopen_targets),
        *_render_candidate_tool_section(candidate_tools),
        *_render_repair_required_tool_section(repair_required_tools),
        *_render_repair_required_skill_section(repair_required_skills),
        _render_stage_summary(payload.get('stage_summary')),
        _render_exec_runtime_policy(payload.get('exec_runtime_policy')),
    ]
    return '\n'.join(lines)


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

    def to_message_payload(self) -> dict[str, Any]:
        payload = {
            'message_type': FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
            'callable_tool_names': list(self.callable_tool_names),
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
    )


def is_frontdoor_tool_contract_message(message: dict[str, Any]) -> bool:
    if frontdoor_tool_contract_payload_from_message(message) is not None:
        return True
    if _frontdoor_message_declares_tool_calls(message):
        return False
    # 注入的契约消息经 sanitize 后只保留 role/content，payload 键被剥离，需靠抬头识别。
    # 契约现在以 system 角色注入；模型回显的契约残留仍是 assistant 文本，两类都识别。
    if str((message or {}).get('role') or '').strip().lower() not in {'assistant', 'system'}:
        return False
    content = str((message or {}).get('content') or '').strip()
    return content.startswith(FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING)


def _frontdoor_tool_contract_heading_index(text: str) -> int:
    """Return the start of a rendered contract embedded in ``text``.

    The heading alone is not enough to classify ordinary user prose that
    happens to mention the contract.  Requiring the canonical kind marker in
    the nearby suffix keeps this helper focused on the provider-facing
    contract summary that the runtime injects.
    """
    normalized = str(text or '')
    heading_index = normalized.find(FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING)
    if heading_index < 0:
        return -1
    suffix = normalized[heading_index : heading_index + 512]
    if f'kind: {FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND}' not in suffix:
        return -1
    return heading_index


def is_frontdoor_tool_contract_echo_text(text: Any) -> bool:
    """Whether model/channel text is a standalone injected tool-contract echo."""
    normalized = str(text or '').strip()
    if not normalized:
        return False
    if normalized.startswith(FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING):
        return _frontdoor_tool_contract_heading_index(normalized) == 0
    return is_frontdoor_tool_contract_message({'role': 'assistant', 'content': normalized})


def strip_frontdoor_tool_contract_echo(text: Any) -> str:
    """Remove a rendered runtime-tool-contract echo from user-facing text.

    A standalone contract echo becomes empty.  If a model puts a visible
    answer before the echoed block, preserve that answer and remove only the
    internal suffix.  JSON contract payloads are handled by the standalone
    classifier above.
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
    updated: list[dict[str, Any]] = []
    replaced = False
    for message in list(messages or []):
        if is_frontdoor_tool_contract_message(message):
            if not replaced:
                updated.append(contract.to_message())
                replaced = True
            continue
        updated.append(dict(message))
    if not replaced:
        updated.append(contract.to_message())
    return updated
