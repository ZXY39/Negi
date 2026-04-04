from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from g3ku.content import parse_content_envelope
from main.models import NodeRecord, normalize_execution_stage_metadata
from main.monitoring.models import TaskProjectionToolResultRecord

_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}


def build_execution_trace(
    node: NodeRecord,
    *,
    tool_results: list[TaskProjectionToolResultRecord] | None = None,
    live_tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    tool_result_map = _tool_result_map(tool_results)
    live_tool_call_map = _live_tool_call_map(live_tool_calls)
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
            record = tool_result_map.get(tool_call_id)
            live_state = dict(live_tool_call_map.get(tool_call_id) or {})
            payload = dict(record.payload.get('parsed_payload') or {}) if record is not None else {}
            arguments = call.get('arguments')
            if isinstance(arguments, (dict, list)):
                arguments_text = json.dumps(arguments, ensure_ascii=False, indent=2)
            else:
                arguments_text = str(arguments or '')
            output_text = str(record.output_preview_text or '') if record is not None else ''
            output_ref = _execution_trace_output_ref(record) if record is not None else ''

            if tool_name in _CONTROL_TOOL_NAMES:
                execution_id = str((payload or {}).get('execution_id') or '').strip()
                if execution_id and execution_id in background_steps:
                    _merge_background_execution_update(
                        background_steps[execution_id],
                        payload=payload or {},
                        record=record,
                        live_state=live_state,
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
                'status': _tool_step_status(
                    output_text,
                    node.status,
                    payload=payload,
                    recorded_status=str(record.status or '') if record is not None else '',
                    live_status=str(live_state.get('status') or ''),
                ),
                'started_at': str(
                    (record.started_at if record is not None else '')
                    or str(live_state.get('started_at') or '')
                    or str(entry.created_at or '')
                ),
                'finished_at': str(
                    (record.finished_at if record is not None else '')
                    or str(live_state.get('finished_at') or '')
                    or ''
                ),
            }
            step['elapsed_seconds'] = _resolve_tool_elapsed_seconds(
                raw_elapsed=record.elapsed_seconds if record is not None else live_state.get('elapsed_seconds'),
                started_at=str(step.get('started_at') or entry.created_at or ''),
                finished_at=str(step.get('finished_at') or ''),
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
                        'status': _tool_step_status(
                            '',
                            node.status,
                            payload={},
                            recorded_status='',
                            live_status=str((live_tool_call_map.get(str(call_id or '').strip()) or {}).get('status') or ''),
                        ),
                        'started_at': str(round_item.created_at or ''),
                        'finished_at': str((live_tool_call_map.get(str(call_id or '').strip()) or {}).get('finished_at') or ''),
                        'elapsed_seconds': _resolve_tool_elapsed_seconds(
                            raw_elapsed=(live_tool_call_map.get(str(call_id or '').strip()) or {}).get('elapsed_seconds'),
                            started_at=str((live_tool_call_map.get(str(call_id or '').strip()) or {}).get('started_at') or round_item.created_at or ''),
                            finished_at=str((live_tool_call_map.get(str(call_id or '').strip()) or {}).get('finished_at') or ''),
                            is_running=str((live_tool_call_map.get(str(call_id or '').strip()) or {}).get('status') or '').strip().lower() in {'queued', 'running'},
                        ),
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
                'stage_kind': str(stage.stage_kind or 'normal'),
                'system_generated': bool(stage.system_generated),
                'mode': str(stage.mode or ''),
                'status': str(stage.status or ''),
                'stage_goal': str(stage.stage_goal or ''),
                'completed_stage_summary': str(stage.completed_stage_summary or ''),
                'key_refs': [item.model_dump(mode='json') for item in list(stage.key_refs or [])],
                'archive_ref': str(stage.archive_ref or ''),
                'archive_stage_index_start': int(stage.archive_stage_index_start or 0),
                'archive_stage_index_end': int(stage.archive_stage_index_end or 0),
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
        'live_tool_calls': [dict(item) for item in list(live_tool_calls or []) if isinstance(item, dict)],
        'live_child_pipelines': [],
        'final_output': str(node.final_output or ''),
        'final_output_ref': str(getattr(node, 'final_output_ref', '') or ''),
        'acceptance_result': str(node.check_result or ''),
        'acceptance_result_ref': str(getattr(node, 'check_result_ref', '') or ''),
    }


def _tool_result_map(tool_results: list[TaskProjectionToolResultRecord] | None) -> dict[str, TaskProjectionToolResultRecord]:
    result: dict[str, TaskProjectionToolResultRecord] = {}
    for item in list(tool_results or []):
        tool_call_id = str(getattr(item, 'tool_call_id', '') or '').strip()
        if tool_call_id:
            result[tool_call_id] = item
    return result


def _live_tool_call_map(live_tool_calls: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in list(live_tool_calls or []):
        if not isinstance(item, dict):
            continue
        tool_call_id = str(item.get('tool_call_id') or '').strip()
        if tool_call_id:
            result[tool_call_id] = dict(item)
    return result


def _execution_trace_output_ref(record: TaskProjectionToolResultRecord) -> str:
    output_ref = str(getattr(record, 'output_ref', '') or '').strip()
    payload = dict(getattr(record, 'payload', {}) or {})
    parsed_payload = payload.get('parsed_payload')
    envelope = parse_content_envelope(parsed_payload)
    if envelope is not None:
        wrapper_ref = str(envelope.ref or '').strip()
        if wrapper_ref:
            return wrapper_ref
    if isinstance(parsed_payload, dict):
        wrapper_ref = str(
            parsed_payload.get('wrapper_ref')
            or parsed_payload.get('requested_ref')
            or parsed_payload.get('ref')
            or ''
        ).strip()
        resolved_ref = str(parsed_payload.get('resolved_ref') or '').strip()
        if wrapper_ref and wrapper_ref != resolved_ref:
            return wrapper_ref
    return output_ref


def _resolve_tool_elapsed_seconds(
    *,
    raw_elapsed: Any,
    started_at: str,
    finished_at: str,
    is_running: bool,
) -> float | None:
    try:
        if raw_elapsed is not None:
            return round(max(0.0, float(raw_elapsed)), 1)
    except (TypeError, ValueError):
        pass
    started_ts = _iso_to_epoch_seconds(started_at)
    if started_ts is None:
        return None
    finished_ts = _iso_to_epoch_seconds(finished_at)
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
    record: TaskProjectionToolResultRecord | None,
    live_state: dict[str, Any],
    output_text: str,
    output_ref: str,
) -> None:
    status = str(payload.get('status') or live_state.get('status') or '').strip().lower()
    if status == 'completed':
        step['status'] = 'success'
    elif status in {'stopped', 'failed', 'error', 'not_found', 'unavailable'}:
        step['status'] = 'error'
    elif status in {'background_running', 'queued', 'running'}:
        step['status'] = 'running'
    if output_text:
        step['output_text'] = output_text
    if output_ref:
        step['output_ref'] = output_ref
    if str((record.finished_at if record is not None else '') or live_state.get('finished_at') or '').strip():
        step['finished_at'] = str((record.finished_at if record is not None else '') or live_state.get('finished_at') or '')
    elapsed = _resolve_tool_elapsed_seconds(
        raw_elapsed=(record.elapsed_seconds if record is not None else live_state.get('elapsed_seconds')),
        started_at=str(step.get('started_at') or ''),
        finished_at=str(step.get('finished_at') or ''),
        is_running=str(step.get('status') or '') == 'running',
    )
    if elapsed is not None:
        step['elapsed_seconds'] = elapsed


def _tool_step_status(
    output_text: str,
    node_status: str,
    *,
    payload: dict[str, Any] | None = None,
    recorded_status: str = '',
    live_status: str = '',
) -> str:
    normalized_live_status = str(live_status or '').strip().lower()
    if normalized_live_status in {'queued', 'running'}:
        return 'running'
    if normalized_live_status == 'success':
        return 'success'
    if normalized_live_status == 'error':
        return 'error'
    normalized_recorded_status = str(recorded_status or '').strip().lower()
    if normalized_recorded_status in {'queued', 'running'}:
        return 'running'
    if normalized_recorded_status == 'success':
        return 'success'
    if normalized_recorded_status == 'error':
        return 'error'
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
