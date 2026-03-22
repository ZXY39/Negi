from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from g3ku.content import content_summary_and_ref
from main.models import NodeRecord, normalize_execution_stage_metadata

_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}


def build_execution_trace(node: NodeRecord) -> dict[str, object]:
    tool_message_map = _tool_message_map(node)
    tool_output_map = _tool_output_map(node)
    tool_ref_map = _tool_output_ref_map(node)
    tool_steps: list[dict[str, Any]] = []
    step_by_call_id: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    background_steps: dict[str, dict[str, Any]] = {}

    for entry in list(node.output or []):
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
            tool_steps.append(step)
            step_by_call_id[tool_call_id] = step

    stage_state = normalize_execution_stage_metadata((node.metadata or {}).get('execution_stages') if isinstance(node.metadata, dict) else {})
    stages: list[dict[str, Any]] = []
    for stage in list(stage_state.stages or []):
        rounds: list[dict[str, Any]] = []
        for round_item in list(stage.rounds or []):
            round_tools: list[dict[str, Any]] = []
            tool_names = list(round_item.tool_names or [])
            for index, call_id in enumerate(list(round_item.tool_call_ids or [])):
                step = copy.deepcopy(step_by_call_id.get(str(call_id or '').strip()) or {})
                if not step:
                    step = {
                        'tool_call_id': str(call_id or '').strip(),
                        'tool_name': str(tool_names[index] if index < len(tool_names) else 'tool'),
                        'arguments_text': '',
                        'output_text': '',
                        'output_ref': '',
                        'status': 'info',
                        'started_at': str(round_item.created_at or ''),
                        'finished_at': '',
                        'elapsed_seconds': None,
                    }
                round_tools.append(step)
            rounds.append(
                {
                    'round_id': str(round_item.round_id or ''),
                    'round_index': int(round_item.round_index or 0),
                    'created_at': str(round_item.created_at or ''),
                    'budget_counted': bool(round_item.budget_counted),
                    'tools': round_tools,
                }
            )
        stages.append(
            {
                'stage_id': str(stage.stage_id or ''),
                'stage_index': int(stage.stage_index or 0),
                'mode': str(stage.mode or ''),
                'status': str(stage.status or ''),
                'stage_goal': str(stage.stage_goal or ''),
                'tool_round_budget': int(stage.tool_round_budget or 0),
                'tool_rounds_used': int(stage.tool_rounds_used or 0),
                'created_at': str(stage.created_at or ''),
                'finished_at': str(stage.finished_at or ''),
                'rounds': rounds,
            }
        )

    return {
        'initial_prompt': str(node.prompt or node.goal or ''),
        'tool_steps': tool_steps,
        'stages': stages,
        'live_tool_calls': [],
        'live_child_pipelines': [],
        'final_output': str(node.final_output or ''),
        'final_output_ref': str(getattr(node, 'final_output_ref', '') or ''),
        'acceptance_result': str(node.check_result or ''),
        'acceptance_result_ref': str(getattr(node, 'check_result_ref', '') or ''),
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
        content = item.get('content')
        summary, _ref = content_summary_and_ref(content)
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
    started_ts = _iso_to_epoch_seconds(started_at)
    if started_ts is None:
        return None
    finished_ts = _iso_to_epoch_seconds(str(message_meta.get('finished_at') or ''))
    if finished_ts is not None:
        return round(max(0.0, finished_ts - started_ts), 1)
    if is_running:
        return round(max(0.0, datetime.now(timezone.utc).timestamp() - started_ts), 1)
    return None


def _iso_to_epoch_seconds(value: str) -> float | None:
    text = str(value or '').strip()
    if not text:
        return None
    normalized = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.timestamp()
    return parsed.astimezone(timezone.utc).timestamp()


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
    elapsed = _resolve_tool_elapsed_seconds(
        message_meta=message_meta,
        payload=payload,
        started_at=str(step.get('started_at') or ''),
        is_running=str(step.get('status') or '') == 'running',
    )
    if elapsed is not None:
        step['elapsed_seconds'] = elapsed


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
