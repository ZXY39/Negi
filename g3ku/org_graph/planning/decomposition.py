from __future__ import annotations

import re
from dataclasses import asdict, replace
from typing import Any, Iterable

from g3ku.org_graph.planning.depth_policy import can_delegate
from g3ku.org_graph.planning.stage_plan import (
    ExecutionPlan,
    StagePlan,
    ValidationBindingPlan,
    ValidationProfilePlan,
    WorkUnitPlan,
)
from g3ku.org_graph.prompt_loader import load_prompt


PLANNER_SYSTEM_PROMPT = '\n\n'.join(
    [
        load_prompt('execution_planning.md'),
        load_prompt('execution_delegation.md'),
        load_prompt('capability_planning.md'),
    ]
)
DEFAULT_ACCEPTANCE_TEMPLATE = '该子项输出必须直接满足本子项 objective_summary，并且可被父节点后续汇总使用。'
_SELECTOR_PATTERN = re.compile(r'^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$')


def _truncate(text: str, limit: int = 120) -> str:
    text = ' '.join(str(text or '').split())
    return text[:limit] if len(text) <= limit else text[: limit - 1] + '...'


def _normalize_names(raw: Any, *, allowed: set[str] | None = None) -> list[str]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = list(raw)
    else:
        values = []
    seen: set[str] = set()
    normalized: list[str] = []
    for item in values:
        value = str(item or '').strip()
        if not value or value in seen:
            continue
        if allowed is not None and allowed and value not in allowed:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def expand_validation_selector(selector: str, *, work_item_count: int) -> list[int]:
    cleaned = str(selector or '').replace(' ', '')
    if not cleaned or not _SELECTOR_PATTERN.fullmatch(cleaned):
        raise ValueError(f'Invalid selector: {selector!r}')
    indexes: set[int] = set()
    for chunk in cleaned.split(','):
        if '-' in chunk:
            start_text, end_text = chunk.split('-', 1)
            start = int(start_text)
            end = int(end_text)
            if start <= 0 or end <= 0 or start > end:
                raise ValueError(f'Invalid selector range: {selector!r}')
            for index in range(start, end + 1):
                if index > work_item_count:
                    raise ValueError(f'Selector out of range: {selector!r}')
                indexes.add(index)
            continue
        index = int(chunk)
        if index <= 0 or index > work_item_count:
            raise ValueError(f'Selector out of range: {selector!r}')
        indexes.add(index)
    return sorted(indexes)


def _default_acceptance_criteria(objective_summary: str) -> str:
    objective = str(objective_summary or '').strip()
    if not objective:
        return DEFAULT_ACCEPTANCE_TEMPLATE
    return DEFAULT_ACCEPTANCE_TEMPLATE.replace('objective_summary', objective)


def _fallback_validation_spec(work_units: list[WorkUnitPlan]) -> tuple[list[ValidationProfilePlan], list[ValidationBindingPlan], list[WorkUnitPlan]]:
    profiles: list[ValidationProfilePlan] = []
    bindings: list[ValidationBindingPlan] = []
    normalized_units: list[WorkUnitPlan] = []
    for index, work_unit in enumerate(work_units, start=1):
        profile_id = f'vp_{index}'
        profiles.append(
            ValidationProfilePlan(
                profile_id=profile_id,
                acceptance_criteria=_default_acceptance_criteria(work_unit.objective_summary),
                validation_tools=[],
            )
        )
        bindings.append(ValidationBindingPlan(selector=str(index), validation_profile_id=profile_id))
        normalized_units.append(replace(work_unit, validation_profile_id=profile_id))
    return profiles, bindings, normalized_units


def _coerce_validation_profile(item: Any, *, allowed_tools: set[str]) -> ValidationProfilePlan | None:
    if not isinstance(item, dict):
        return None
    profile_id = str(item.get('profile_id') or '').strip()
    acceptance_criteria = str(item.get('acceptance_criteria') or '').strip()
    if not profile_id or not acceptance_criteria:
        return None
    validation_tools = _normalize_names(item.get('validation_tools') or [], allowed=allowed_tools)
    return ValidationProfilePlan(
        profile_id=profile_id,
        acceptance_criteria=acceptance_criteria,
        validation_tools=validation_tools,
    )


def _coerce_validation_binding(item: Any) -> ValidationBindingPlan | None:
    if not isinstance(item, dict):
        return None
    selector = str(item.get('selector') or '').strip()
    validation_profile_id = str(item.get('validation_profile_id') or '').strip()
    if not selector or not validation_profile_id:
        return None
    return ValidationBindingPlan(selector=selector, validation_profile_id=validation_profile_id)


def normalize_stage_validation(
    work_units: list[WorkUnitPlan],
    *,
    validation_profiles: list[ValidationProfilePlan],
    validation_bindings: list[ValidationBindingPlan],
) -> tuple[list[ValidationProfilePlan], list[ValidationBindingPlan], list[WorkUnitPlan]]:
    if not work_units:
        return [], [], []
    if not validation_profiles or not validation_bindings:
        return _fallback_validation_spec(work_units)
    profile_map: dict[str, ValidationProfilePlan] = {}
    ordered_profiles: list[ValidationProfilePlan] = []
    for profile in validation_profiles:
        if profile.profile_id in profile_map:
            return _fallback_validation_spec(work_units)
        profile_map[profile.profile_id] = profile
        ordered_profiles.append(profile)
    coverage: dict[int, str] = {}
    try:
        for binding in validation_bindings:
            if binding.validation_profile_id not in profile_map:
                raise ValueError('Unknown validation profile reference')
            indexes = expand_validation_selector(binding.selector, work_item_count=len(work_units))
            for index in indexes:
                if index in coverage:
                    raise ValueError('Overlapping validation bindings')
                coverage[index] = binding.validation_profile_id
        expected_indexes = set(range(1, len(work_units) + 1))
        if set(coverage) != expected_indexes:
            raise ValueError('Validation bindings do not fully cover work items')
    except ValueError:
        return _fallback_validation_spec(work_units)
    normalized_units = [
        replace(work_unit, validation_profile_id=coverage[index])
        for index, work_unit in enumerate(work_units, start=1)
    ]
    return ordered_profiles, list(validation_bindings), normalized_units


def _fallback_plan(text: str) -> ExecutionPlan:
    work_unit = WorkUnitPlan(
        role_title='当前工作单元',
        objective_summary=text,
        prompt_preview=f'围绕以下目标直接执行并产出结果：{_truncate(text)}',
        mode='local',
    )
    validation_profiles, validation_bindings, work_units = _fallback_validation_spec([work_unit])
    return ExecutionPlan(
        stages=[
            StagePlan(
                title='执行当前目标',
                objective_summary=_truncate(text),
                dispatch_shape='single',
                work_units=work_units,
                validation_profiles=validation_profiles,
                validation_bindings=validation_bindings,
            )
        ]
    )


def _coerce_work_unit(
    item: dict[str, Any],
    *,
    delegation_allowed: bool,
) -> WorkUnitPlan | None:
    role_title = str(item.get('role_title') or item.get('title') or '').strip()
    objective_summary = str(item.get('objective_summary') or item.get('objective') or '').strip()
    prompt_preview = str(item.get('prompt_preview') or item.get('prompt') or objective_summary or role_title).strip()
    if not role_title or not objective_summary:
        return None
    mode = str(item.get('mode') or 'local').strip().lower()
    if mode not in {'local', 'delegate'}:
        mode = 'local'
    if mode == 'delegate' and not delegation_allowed:
        mode = 'local'
    provider_model = str(item.get('provider_model') or '').strip() or None
    mutation_allowed = bool(item.get('mutation_allowed', False))
    return WorkUnitPlan(
        role_title=role_title,
        objective_summary=objective_summary,
        prompt_preview=prompt_preview,
        mode=mode,
        provider_model=provider_model,
        mutation_allowed=mutation_allowed,
        validation_profile_id=None,
    )


def _coerce_plan(
    payload: dict[str, Any],
    *,
    delegation_allowed: bool,
    suggested_tool_names: set[str],
) -> ExecutionPlan | None:
    raw_stages = payload.get('stages')
    if not isinstance(raw_stages, list) or not raw_stages:
        return None
    stages: list[StagePlan] = []
    for raw_stage in raw_stages[:6]:
        if not isinstance(raw_stage, dict):
            continue
        title = str(raw_stage.get('title') or '').strip() or '执行'
        objective_summary = str(raw_stage.get('objective_summary') or raw_stage.get('objective') or title).strip()
        dispatch_shape = str(raw_stage.get('dispatch_shape') or 'single').strip().lower()
        if dispatch_shape not in {'single', 'parallel'}:
            dispatch_shape = 'single'
        raw_work_units = raw_stage.get('work_units')
        if not isinstance(raw_work_units, list):
            raw_work_units = []
        work_units = [
            work_unit
            for work_unit in (
                _coerce_work_unit(
                    item,
                    delegation_allowed=delegation_allowed,
                )
                for item in raw_work_units
            )
            if work_unit is not None
        ]
        if not work_units:
            work_units = [
                WorkUnitPlan(
                    role_title='当前工作单元',
                    objective_summary=objective_summary,
                    prompt_preview=objective_summary,
                    mode='local',
                )
            ]
        if len(work_units) == 1:
            dispatch_shape = 'single'
        raw_profiles = raw_stage.get('validation_profiles')
        raw_bindings = raw_stage.get('validation_bindings')
        validation_profiles = [
            profile
            for profile in (
                _coerce_validation_profile(item, allowed_tools=suggested_tool_names)
                for item in (raw_profiles if isinstance(raw_profiles, list) else [])
            )
            if profile is not None
        ]
        validation_bindings = [
            binding
            for binding in (
                _coerce_validation_binding(item)
                for item in (raw_bindings if isinstance(raw_bindings, list) else [])
            )
            if binding is not None
        ]
        validation_profiles, validation_bindings, work_units = normalize_stage_validation(
            work_units,
            validation_profiles=validation_profiles,
            validation_bindings=validation_bindings,
        )
        stages.append(
            StagePlan(
                title=title,
                objective_summary=objective_summary,
                dispatch_shape=dispatch_shape,
                work_units=work_units,
                validation_profiles=validation_profiles,
                validation_bindings=validation_bindings,
            )
        )
    return ExecutionPlan(stages=stages) if stages else None


async def build_execution_plan(
    objective: str,
    *,
    level: int,
    effective_max_depth: int,
    llm=None,
    provider_model: str | None = None,
    provider_model_chain: list[str] | None = None,
    local_available_tools: list[str] | None = None,
    local_available_skills: list[str] | None = None,
    delegate_available_tools: list[str] | None = None,
    delegate_available_skills: list[str] | None = None,
    monitor_context: dict[str, Any] | None = None,
) -> ExecutionPlan:
    text = str(objective or '').strip() or '未命名目标'
    delegation_allowed = can_delegate(level, effective_max_depth)
    local_tools = set(_normalize_names(local_available_tools or []))
    local_skills = set(_normalize_names(local_available_skills or []))
    delegate_tools = set(_normalize_names(delegate_available_tools or []))
    delegate_skills = set(_normalize_names(delegate_available_skills or []))
    if llm is not None:
        tool_text = '、'.join(sorted(local_tools)) if local_tools else '无'
        skill_text = '、'.join(sorted(local_skills)) if local_skills else '无'
        delegate_tool_text = '、'.join(sorted(delegate_tools)) if delegate_tools else '无'
        delegate_skill_text = '、'.join(sorted(delegate_skills)) if delegate_skills else '无'
        user_prompt = (
            f'目标：\n{text}\n\n'
            f'当前层级：{level}\n'
            f'有效最大深度：{effective_max_depth}\n'
            f'当前是否允许继续派生：{delegation_allowed}\n'
            f'当前执行节点可用工具：{tool_text}\n'
            f'当前执行节点可用技能：{skill_text}\n'
            f'派生子执行节点时可用工具：{delegate_tool_text}\n'
            f'派生子执行节点时可用技能：{delegate_skill_text}\n\n'
            '只返回一个 JSON 对象，顶层必须是 `stages` 数组。'
        )
        try:
            payload = await llm.chat_json(
                system_prompt=PLANNER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                provider_model=provider_model,
                provider_model_chain=provider_model_chain,
                monitor_context=monitor_context,
            )
            if payload:
                plan = _coerce_plan(
                    payload,
                    delegation_allowed=delegation_allowed,
                    suggested_tool_names=local_tools | delegate_tools,
                )
                if plan is not None:
                    return plan
        except Exception:
            pass
    plan = _fallback_plan(text)
    ctx = monitor_context if isinstance(monitor_context, dict) else {}
    service = ctx.get('service')
    project = ctx.get('project')
    unit = ctx.get('unit')
    if service is not None and project is not None and unit is not None:
        service.monitor_service.record_output(
            project=project,
            unit=unit,
            content=json.dumps(asdict(plan), ensure_ascii=False, indent=2),
            kind='output',
            meta={'source': 'fallback_plan'},
        )
    return plan


