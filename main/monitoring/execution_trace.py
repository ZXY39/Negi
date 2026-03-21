from __future__ import annotations

import json
from typing import Any

from g3ku.content import content_summary_and_ref
from main.models import NodeRecord


_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}


def build_execution_trace(node: NodeRecord) -> dict[str, object]:
    tool_message_map = _tool_message_map(node)
    tool_output_map = _tool_output_map(node)
    tool_ref_map = _tool_output_ref_map(node)
    tool_steps: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    background_steps: dict[str, dict[str, Any]] = {}

    for entry in list(node.output or []):
        round_metadata = dict(getattr(entry, 'round_metadata', {}) or {})
        round_index = _coerce_positive_int(round_metadata.get('round_index'), fallback=int(getattr(entry, 'seq', 0) or 0))
        spawn_precheck = _normalize_spawn_precheck(round_metadata.get('spawn_precheck'))
        round_is_blocked = spawn_precheck.get('valid') is False or bool(spawn_precheck.get('violation_codes'))
        round_tools: list[dict[str, Any]] = []

        for call in list(entry.tool_calls or []):
            tool_call_id = str(call.get('id') or '').strip()
            if not tool_call_id or tool_call_id in seen_ids:
                continue
            seen_ids.add(tool_call_id)
            tool_name = str(call.get('name') or 'tool')
            output_text = str(tool_output_map.get(tool_call_id) or '')
            output_ref = str(tool_ref_map.get(tool_call_id) or '')
            message_meta = dict(tool_message_map.get(tool_call_id) or {})
            payload = _parse_tool_payload(message_meta.get('content'))
            arguments = call.get('arguments')
            if isinstance(arguments, (dict, list)):
                arguments_text = json.dumps(arguments, ensure_ascii=False, indent=2)
            else:
                arguments_text = str(arguments or '')

            if tool_name in _CONTROL_TOOL_NAMES:
                execution_id = str((payload or {}).get('execution_id') or '').strip()
                if execution_id and execution_id in background_steps:
                    _merge_background_execution_update(
                        background_steps[execution_id],
                        payload=payload or {},
                        message_meta=message_meta,
                        output_text=output_text,
                        output_ref=output_ref,
                    )
                continue
            if tool_name == 'spawn_precheck':
                continue
            if round_is_blocked:
                continue

            step: dict[str, Any] = {
                'tool_call_id': tool_call_id,
                'tool_name': tool_name,
                'arguments_text': arguments_text,
                'output_text': output_text,
                'output_ref': output_ref,
                'status': _tool_step_status(output_text, node.status, payload=payload),
                'started_at': str(entry.created_at or ''),
                'finished_at': str(message_meta.get('finished_at') or ''),
            }
            step['elapsed_seconds'] = _resolve_tool_elapsed_seconds(
                message_meta=message_meta,
                payload=payload,
                started_at=str(entry.created_at or ''),
                is_running=step['status'] == 'running',
            )
            execution_id = str((payload or {}).get('execution_id') or '').strip()
            if execution_id:
                step['execution_id'] = execution_id
                background_steps[execution_id] = step
            round_tools.append(step)
            tool_steps.append(step)

        rounds.append(
            {
                'round_index': round_index,
                'created_at': str(entry.created_at or ''),
                'assistant_summary': str(entry.content or ''),
                'spawn_precheck': spawn_precheck,
                'tools': round_tools,
            }
        )

    return {
        'initial_prompt': str(node.prompt or node.goal or ''),
        'rounds': rounds,
        'tool_steps': tool_steps,
        'live_tool_calls': [],
        'live_child_pipelines': [],
        'final_output': str(node.final_output or ''),
        'final_output_ref': str(getattr(node, 'final_output_ref', '') or ''),
        'acceptance_result': str(node.check_result or ''),
        'acceptance_result_ref': str(getattr(node, 'check_result_ref', '') or ''),
    }


def _coerce_positive_int(value: Any, *, fallback: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = int(fallback or 0)
    return resolved if resolved > 0 else max(1, int(fallback or 1))


def _normalize_spawn_precheck(value: Any) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, dict) else {}
    rule_ids = [int(item) for item in list(payload.get('rule_ids') or []) if isinstance(item, int)]
    violation_codes = [
        str(item or '').strip()
        for item in list(payload.get('violation_codes') or [])
        if str(item or '').strip()
    ]
    return {
        'present': bool(payload.get('present')),
        'valid': not violation_codes if 'valid' not in payload else bool(payload.get('valid')),
        'decision': str(payload.get('decision') or '').strip(),
        'reason': str(payload.get('reason') or '').strip(),
        'rule_ids': rule_ids,
        'rule_semantics': str(payload.get('rule_semantics') or '').strip(),
        'violation_codes': violation_codes,
    }


def _parse_input_messages(raw: str) -> list[dict[str, object]]:
    text = str(raw or '').strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _tool_message_map(node: NodeRecord) -> dict[str, dict[str, Any]]:
    messages = _parse_input_messages(node.input)
    result: dict[str, dict[str, Any]] = {}
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get('role') or '').strip().lower() != 'tool':
            continue
        tool_call_id = str(item.get('tool_call_id') or '').strip()
        if not tool_call_id:
            continue
        result[tool_call_id] = dict(item)
    return result


def _tool_output_map(node: NodeRecord) -> dict[str, str]:
    messages = _parse_input_messages(node.input)
    result: dict[str, str] = {}
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get('role') or '').strip().lower() != 'tool':
            continue
        tool_call_id = str(item.get('tool_call_id') or '').strip()
        if not tool_call_id:
            continue
        summary, _ref = content_summary_and_ref(item.get('content'))
        result[tool_call_id] = summary
    return result


def _tool_output_ref_map(node: NodeRecord) -> dict[str, str]:
    messages = _parse_input_messages(node.input)
    result: dict[str, str] = {}
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get('role') or '').strip().lower() != 'tool':
            continue
        tool_call_id = str(item.get('tool_call_id') or '').strip()
        if not tool_call_id:
            continue
        _summary, ref = content_summary_and_ref(item.get('content'))
        result[tool_call_id] = ref
    return result


def _parse_tool_payload(content: object) -> dict[str, object] | None:
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text[:1] not in {'{', '['}:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _resolve_tool_elapsed_seconds(
    *,
    message_meta: dict[str, Any],
    payload: dict[str, Any] | None,
    started_at: str,
    is_running: bool,
) -> float | None:
    raw_elapsed = None
    if isinstance(payload, dict):
        raw_elapsed = payload.get('elapsed_seconds')
    if raw_elapsed is None:
        raw_elapsed = message_meta.get('elapsed_seconds')
    try:
        if raw_elapsed is not None:
            return round(max(0.0, float(raw_elapsed)), 1)
    except (TypeError, ValueError):
        pass
    _ = started_at, is_running
    return None


def _merge_background_execution_update(
    step: dict[str, Any],
    *,
    payload: dict[str, Any],
    message_meta: dict[str, Any],
    output_text: str,
    output_ref: str,
) -> None:
    status = str(payload.get('status') or '').strip().lower()
    if status == 'completed':
        step['status'] = 'success'
    elif status in {'stopped', 'failed', 'error', 'not_found', 'unavailable'}:
        step['status'] = 'error'
    elif status == 'background_running':
        step['status'] = 'running'
    if output_text:
        step['output_text'] = output_text
    if output_ref:
        step['output_ref'] = output_ref
    if str(message_meta.get('finished_at') or '').strip():
        step['finished_at'] = str(message_meta.get('finished_at') or '')


def _tool_step_status(output_text: str, node_status: str, *, payload: dict[str, Any] | None = None) -> str:
    payload_status = str((payload or {}).get('status') or '').strip().lower()
    if payload_status == 'background_running':
        return 'running'
    if payload_status in {'completed'}:
        return 'success'
    if payload_status in {'stopped', 'failed', 'error', 'not_found', 'unavailable'}:
        return 'error'
    text = str(output_text or '').strip()
    if text:
        lowered = text.lower()
        if text.startswith('Error:') or '"status":"error"' in lowered or '"status": "error"' in lowered:
            return 'error'
        return 'success'
    if str(node_status or '').strip().lower() == 'in_progress':
        return 'running'
    return 'success'
