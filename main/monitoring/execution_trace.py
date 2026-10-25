from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from g3ku.content import parse_content_envelope
from g3ku.runtime.tool_impulse import CONTRACT_DELIVERY_FIELD
from main.models import NodeRecord
from main.monitoring.models import TaskProjectionToolResultRecord

_CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}

# 轮次行往外带的思考读数只有这四个标量。
THINKING_READING_FIELDS = ('seconds', 'chars', 'est_tokens', 'tokens')


def thinking_reading_for_display(value: Any) -> dict[str, Any]:
    """把轮次上的思考读数摘成可外发的那一份。

    思考全文住在 `reasoning_content` 上，从不进节点详情的载荷：这里按字段名逐个摘，
    写侧多出来的键不会跟着走到界面。缺键就是这一族口径这次没上报，不补 0。
    """
    if not isinstance(value, dict):
        return {}
    reading: dict[str, Any] = {}
    for field in THINKING_READING_FIELDS:
        if field not in value:
            continue
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            continue
        reading[field] = number
    return reading


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
            record_payload = dict(record.payload or {}) if record is not None else {}
            payload = dict(record_payload.get('parsed_payload') or {})
            contract_delivery = str(
                record_payload.get(CONTRACT_DELIVERY_FIELD)
                or live_state.get(CONTRACT_DELIVERY_FIELD)
                or ''
            ).strip()
            # 入参的家是投影行；行内 arguments 只在"这一拍刚写、还没执行回来"和迁移前的
            # 存量条目里存在，两种都由下面的兜底读到。
            arguments_text = str(record.arguments_text or '') if record is not None else ''
            if not arguments_text.strip():
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
                **({CONTRACT_DELIVERY_FIELD: contract_delivery} if contract_delivery else {}),
            }
            if isinstance(payload, dict):
                step['recovery_decision'] = str(payload.get('recovery_decision') or '').strip()
                step['related_tool_call_ids'] = [
                    str(item or '').strip()
                    for item in list(payload.get('related_tool_call_ids') or [])
                    if str(item or '').strip()
                ]
                step['attempted_tools'] = [
                    str(item or '').strip()
                    for item in list(payload.get('attempted_tools') or [])
                    if str(item or '').strip()
                ]
                step['evidence'] = [
                    dict(item) for item in list(payload.get('evidence') or []) if isinstance(item, dict)
                ]
                step['lost_result_summary'] = str(payload.get('lost_result_summary') or '').strip()
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

    # 运行时补写的工具步（恢复核验、恢复裁决）没有模型调用行，turn 里挂不进去；
    # 但它们仍是操作员判断"这一步凭什么算完成"的证据，按 order 补进扁平列表。
    standalone_steps: list[tuple[int, dict[str, Any]]] = []
    for record in list(tool_results or []):
        call_id = str(getattr(record, 'tool_call_id', '') or '').strip()
        if not call_id or call_id in seen_ids:
            continue
        step = _standalone_tool_result_step(record)
        if step is not None:
            standalone_steps.append((int(getattr(record, 'order_index', 0) or 0), step))
    tool_steps.extend(step for _, step in sorted(standalone_steps, key=lambda item: item[0]))

    turns = _build_execution_turns(node, step_by_call_id)
    return {
        'initial_prompt': str(node.prompt or node.goal or ''),
        'turns': turns,
        'tool_steps': tool_steps,
        'live_tool_calls': [dict(item) for item in list(live_tool_calls or []) if isinstance(item, dict)],
        'live_child_pipelines': [],
        'final_output': str(node.final_output or ''),
        'final_output_ref': str(getattr(node, 'final_output_ref', '') or ''),
        'acceptance_result': str(node.check_result or ''),
        'acceptance_result_ref': str(getattr(node, 'check_result_ref', '') or ''),
    }


def _standalone_tool_result_step(
    record: TaskProjectionToolResultRecord,
) -> dict[str, Any] | None:
    tool_call_id = str(getattr(record, 'tool_call_id', '') or '').strip()
    if not tool_call_id:
        return None
    record_payload = dict(getattr(record, 'payload', None) or {})
    payload = dict(record_payload.get('parsed_payload') or {})
    status = _tool_step_status(
        str(getattr(record, 'output_preview_text', '') or ''),
        '',
        payload=payload,
        recorded_status=str(getattr(record, 'status', '') or ''),
    )
    step: dict[str, Any] = {
        'tool_call_id': tool_call_id,
        'tool_name': str(getattr(record, 'tool_name', '') or '').strip() or 'tool',
        'arguments_text': str(getattr(record, 'arguments_text', '') or ''),
        'output_text': str(getattr(record, 'output_preview_text', '') or ''),
        'output_ref': _execution_trace_output_ref(record),
        'status': status,
        'started_at': str(getattr(record, 'started_at', '') or ''),
        'finished_at': str(getattr(record, 'finished_at', '') or ''),
        'kind': str(payload.get('kind') or ''),
        'round_id': str(payload.get('round_id') or ''),
        'recovery_decision': str(payload.get('recovery_decision') or '').strip(),
        'related_tool_call_ids': [
            str(item or '').strip()
            for item in list(payload.get('related_tool_call_ids') or [])
            if str(item or '').strip()
        ],
        'attempted_tools': [
            str(item or '').strip()
            for item in list(payload.get('attempted_tools') or [])
            if str(item or '').strip()
        ],
        'evidence': [dict(item) for item in list(payload.get('evidence') or []) if isinstance(item, dict)],
        'lost_result_summary': str(payload.get('lost_result_summary') or '').strip(),
    }
    step['elapsed_seconds'] = _resolve_tool_elapsed_seconds(
        raw_elapsed=getattr(record, 'elapsed_seconds', None),
        started_at=step['started_at'],
        finished_at=step['finished_at'],
        is_running=status == 'running',
    )
    return step


def _build_execution_turns(
    node: NodeRecord,
    step_by_call_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for fallback_index, entry in enumerate(list(node.output or []), start=1):
        seq = int(getattr(entry, 'seq', 0) or fallback_index)
        tools: list[dict[str, Any]] = []
        for call in list(getattr(entry, 'tool_calls', []) or []):
            call_id = str((call or {}).get('id') or '').strip() if isinstance(call, dict) else ''
            step = step_by_call_id.get(call_id) if call_id else None
            if step is not None:
                tools.append(copy.deepcopy(step))
        turns.append(
            {
                'seq': seq,
                'text': str(getattr(entry, 'content', '') or ''),
                'text_ref': str(getattr(entry, 'content_ref', '') or ''),
                'created_at': str(getattr(entry, 'created_at', '') or ''),
                'thinking': thinking_reading_for_display(getattr(entry, 'thinking', None)),
                'status': (
                    'running'
                    if any(
                        str(tool.get('status') or '').strip().lower() in {'queued', 'running'}
                        for tool in tools
                    )
                    else 'completed'
                ),
                'tools': tools,
            }
        )
    return turns


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
            parsed_payload.get('output_ref')
            or parsed_payload.get('wrapper_ref')
            or parsed_payload.get('requested_ref')
            or parsed_payload.get('ref')
            or ''
        ).strip()
        resolved_ref = str(parsed_payload.get('resolved_ref') or '').strip()
        if wrapper_ref and wrapper_ref != resolved_ref:
            return wrapper_ref
        nested = parse_content_envelope(parsed_payload.get('content_ref'))
        if nested is not None:
            nested_ref = str(nested.ref or nested.wrapper_ref or nested.resolved_ref or '').strip()
            if nested_ref:
                return nested_ref
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
    normalized_recorded_status = str(recorded_status or '').strip().lower()
    if normalized_recorded_status in {'queued', 'running'}:
        return 'running'
    if normalized_recorded_status == 'success':
        return 'success'
    if normalized_recorded_status == 'error':
        return 'error'
    if normalized_recorded_status == 'warning':
        return 'warning'
    if normalized_recorded_status == 'interrupted':
        return 'interrupted'
    normalized_live_status = str(live_status or '').strip().lower()
    if normalized_live_status in {'queued', 'running'}:
        return 'running'
    if normalized_live_status == 'success':
        return 'success'
    if normalized_live_status == 'error':
        return 'error'
    if normalized_live_status == 'warning':
        return 'warning'
    if normalized_live_status == 'interrupted':
        return 'interrupted'
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


