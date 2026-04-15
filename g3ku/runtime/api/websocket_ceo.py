from __future__ import annotations

import asyncio
import base64
import mimetypes
import shutil
import uuid
from inspect import isawaitable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect

from g3ku.core.messages import UserInputMessage
from g3ku.core.events import AgentEvent
from g3ku.security import get_bootstrap_security_service
from g3ku.runtime.web_ceo_sessions import (
    WebCeoStateStore,
    build_ceo_session_catalog,
    build_local_ceo_session_item,
    build_session_summary,
    ceo_session_family,
    create_web_ceo_session,
    ensure_active_web_ceo_session,
    ensure_ceo_session_metadata,
    find_ceo_session_catalog_item,
    is_internal_ceo_user_message,
    list_web_ceo_sessions,
    read_inflight_turn_snapshot,
    resolve_execution_snapshot,
    resolve_active_ceo_session_id,
    upload_dir_for_session,
    workspace_path,
)
from g3ku.shells.web import (
    ensure_web_runtime_services,
    get_agent,
    get_runtime_manager,
    is_no_ceo_model_configured_error,
    no_ceo_model_configured_payload,
)
from g3ku.utils.helpers import safe_filename
from main.api.websocket_utils import (
    WebSocketChannelClosed,
    websocket_close,
    websocket_receive_json,
    websocket_send_json,
)
from main.protocol import build_envelope

router = APIRouter()
_HEARTBEAT_OK = "HEARTBEAT_OK"


def _registry(agent):
    service = getattr(agent, 'main_task_service', None)
    return getattr(service, 'registry', None) if service is not None else None


def _runtime_session(runtime_manager, session_id: str):
    getter = getattr(runtime_manager, 'get', None)
    return getter(session_id) if callable(getter) else None


def _session_can_resume_manual_pause(session) -> bool:
    if session is None or not hasattr(session, 'resume'):
        return False
    state = getattr(session, 'state', None)
    status = str(getattr(state, 'status', '') or '').strip().lower()
    pending_interrupts = list(getattr(state, 'pending_interrupts', []) or [])
    if pending_interrupts:
        return False
    if bool(getattr(state, 'paused', False)) or status == 'paused':
        return True
    snapshot_supplier = getattr(session, 'paused_execution_context_snapshot', None)
    if callable(snapshot_supplier):
        try:
            snapshot = snapshot_supplier()
        except Exception:
            snapshot = None
        return isinstance(snapshot, dict) and str(snapshot.get('status') or '').strip().lower() == 'paused'
    return False


def _session_is_running(runtime_manager, session_id: str) -> bool:
    session = _runtime_session(runtime_manager, session_id)
    if session is None:
        return False
    state = getattr(session, 'state', None)
    status = str(getattr(state, 'status', '') or '').strip().lower()
    return bool(getattr(state, 'is_running', False)) or status == 'running'


def _is_channel_session_id(session_id: str) -> bool:
    return str(session_id or '').strip().startswith('china:')


def _publish_ceo_sessions_snapshot(*, agent, transcript_store, runtime_manager, state_store) -> None:
    registry = _registry(agent)
    if registry is None or transcript_store is None:
        return
    active_session_id = resolve_active_ceo_session_id(transcript_store, state_store)
    catalog = build_ceo_session_catalog(
        transcript_store,
        active_session_id=active_session_id,
        is_running_resolver=lambda session_id: _session_is_running(runtime_manager, session_id),
    )
    registry.publish_global_ceo(
        build_envelope(
            channel='ceo',
            session_id=active_session_id or 'web:shared',
            seq=registry.next_ceo_seq(active_session_id or 'web:shared'),
            type='ceo.sessions.snapshot',
            data={
                'items': catalog.get('items') or [],
                'channel_groups': catalog.get('channel_groups') or [],
                'active_session_id': active_session_id,
                'active_session_family': catalog.get('active_session_family') or ceo_session_family(active_session_id),
            },
        )
    )


def _publish_ceo_session_patch(
    *,
    agent,
    transcript_store,
    runtime_manager,
    state_store,
    session_id: str,
    preview_text: str | None = None,
    message_count: int | None = None,
    is_running: bool | None = None,
) -> None:
    registry = _registry(agent)
    if registry is None or transcript_store is None:
        return
    key = str(session_id or '').strip()
    if not key:
        return
    active_session_id = resolve_active_ceo_session_id(transcript_store, state_store)
    item = build_local_ceo_session_item(
        transcript_store,
        key,
        active_session_id=active_session_id,
        is_running=_session_is_running(runtime_manager, key) if is_running is None else bool(is_running),
    )
    if item is None:
        session = transcript_store.get_or_create(key)
        item = build_session_summary(
            session,
            is_active=key == active_session_id,
            is_running=_session_is_running(runtime_manager, key) if is_running is None else bool(is_running),
        )
    if preview_text is not None:
        item['preview_text'] = str(preview_text or '').strip()
    if message_count is not None:
        item['message_count'] = max(0, int(message_count))
    registry.publish_global_ceo(
        build_envelope(
            channel='ceo',
            session_id=key,
            seq=registry.next_ceo_seq(key),
            type='ceo.sessions.patch',
            data={
                'item': item,
                'active_session_id': active_session_id,
                'active_session_family': ceo_session_family(active_session_id),
            },
        )
    )


def _session_upload_dir(session_id: str) -> Path:
    return upload_dir_for_session(session_id)


def _guess_upload_mime_type(name: str, content_type: str | None = None) -> str:
    if isinstance(content_type, str) and content_type.strip():
        return content_type.strip()
    guessed, _ = mimetypes.guess_type(name)
    return guessed or 'application/octet-stream'


def _upload_kind(*, mime_type: str, name: str) -> str:
    if str(mime_type or '').lower().startswith('image/'):
        return 'image'
    guessed, _ = mimetypes.guess_type(name)
    if isinstance(guessed, str) and guessed.lower().startswith('image/'):
        return 'image'
    return 'file'


def _serialize_upload_descriptor(path: Path, *, name: str, mime_type: str) -> dict[str, Any]:
    resolved = path.resolve()
    resolved_mime = _guess_upload_mime_type(name, mime_type)
    return {
        'name': name,
        'path': str(resolved),
        'relative_path': resolved.relative_to(workspace_path()).as_posix(),
        'mime_type': resolved_mime,
        'size': resolved.stat().st_size,
        'kind': _upload_kind(mime_type=resolved_mime, name=name),
    }


async def _maybe_await(value: Any) -> Any:
    if isawaitable(value):
        return await value
    return value


async def _store_uploaded_file(session_id: str, upload: UploadFile) -> dict[str, Any]:
    original_name = safe_filename(str(upload.filename or '').strip()) or 'upload.bin'
    target_dir = _session_upload_dir(session_id)
    target_path = target_dir / f"{uuid.uuid4().hex[:12]}_{original_name}"
    with target_path.open('wb') as handle:
        shutil.copyfileobj(upload.file, handle)
    return _serialize_upload_descriptor(
        target_path,
        name=original_name,
        mime_type=_guess_upload_mime_type(original_name, getattr(upload, 'content_type', None)),
    )


def _resolve_uploaded_file(session_id: str, raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail='invalid_upload_path')
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (workspace_path() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    upload_dir = _session_upload_dir(session_id).resolve()
    try:
        candidate.relative_to(upload_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='upload_path_outside_session_dir') from exc
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail='uploaded_file_not_found')
    return candidate


def _normalize_uploaded_files(session_id: str, uploads_payload: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for raw in list(uploads_payload or []):
        if not isinstance(raw, dict):
            continue
        path = _resolve_uploaded_file(session_id, str(raw.get('path') or ''))
        path_key = str(path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        name = safe_filename(str(raw.get('name') or path.name).strip()) or path.name
        mime_type = _guess_upload_mime_type(
            name,
            str(raw.get('mime_type') or raw.get('mimeType') or '').strip() or None,
        )
        normalized.append(_serialize_upload_descriptor(path, name=name, mime_type=mime_type))
    return normalized


def _uploaded_files_note(uploads: list[dict[str, Any]]) -> str:
    if not uploads:
        return ''
    lines = ['Uploaded attachments:']
    for item in uploads:
        if str(item.get('kind') or '') == 'image':
            lines.append(f"- image: {item['name']} (local path: {item['path']})")
        else:
            lines.append(f"- file: {item['name']} (local path: {item['path']})")
    lines.append('You may inspect the local file paths above when helpful.')
    return "\n".join(lines)


def _image_path_to_data_url(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode('ascii')
    return f"data:{mime_type};base64,{encoded}"


def _build_user_message(text: str, uploads: list[dict[str, Any]]) -> str | UserInputMessage:
    if not uploads:
        return text

    text_value = str(text or '')
    note = _uploaded_files_note(uploads)
    merged_text = f"{text_value}\n\n{note}" if (note and text_value) else (note or text_value)
    content: list[dict[str, Any]] = []
    if merged_text:
        content.append({'type': 'text', 'text': merged_text})
    for item in uploads:
        if str(item.get('kind') or '') != 'image':
            continue
        content.append(
            {
                'type': 'image_url',
                'image_url': {'url': _image_path_to_data_url(Path(str(item['path'])), str(item['mime_type']))},
            }
        )

    return UserInputMessage(
        content=content or [{'type': 'text', 'text': note or text_value}],
        attachments=[str(item['path']) for item in uploads],
        metadata={'web_ceo_uploads': uploads, 'web_ceo_raw_text': text_value},
    )


def _normalize_client_user_messages(session_id: str, payload: dict[str, Any]) -> list[UserInputMessage]:
    batch_payload = payload.get('messages')
    raw_entries = list(batch_payload or []) if isinstance(batch_payload, list) else [payload]
    messages: list[UserInputMessage] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get('text') or '')
        uploads = _normalize_uploaded_files(session_id, raw.get('uploads'))
        if not text.strip() and not uploads:
            continue
        built = _build_user_message(text, uploads)
        if isinstance(built, UserInputMessage):
            messages.append(built)
        else:
            messages.append(UserInputMessage(content=str(built)))
    return messages


def _build_inflight_turn_snapshot(
    session: Any,
    session_id: str,
    persisted_session: Any | None = None,
) -> dict[str, Any] | None:
    snapshot: dict[str, Any] | None = None
    try:
        resolved_snapshot, _resolved_source = resolve_execution_snapshot(session, persisted_session)
        if isinstance(resolved_snapshot, dict):
            snapshot = resolved_snapshot
    except Exception:
        snapshot = None
    if not isinstance(snapshot, dict):
        getter = getattr(session, 'inflight_turn_snapshot', None)
        if not callable(getter):
            snapshot = read_inflight_turn_snapshot(session_id)
        else:
            snapshot = getter()
            if not isinstance(snapshot, dict):
                snapshot = read_inflight_turn_snapshot(session_id)
    if not isinstance(snapshot, dict):
        return None
    return snapshot


@router.post('/ceo/uploads')
async def upload_ceo_files(
    session_id: str = Query('web:shared'),
    files: list[UploadFile] = File(...),
):
    items: list[dict[str, Any]] = []
    for upload in list(files or []):
        try:
            items.append(await _store_uploaded_file(session_id, upload))
        finally:
            await upload.close()
    if not items:
        raise HTTPException(status_code=400, detail='no_files_uploaded')
    return {'ok': True, 'session_id': session_id, 'items': items}


def _coerce_event_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get('data')
    return data if isinstance(data, dict) else {}


def _history_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get('text', item.get('content', ''))
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content or '').strip()


def _normalize_snapshot_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    uploads = metadata.get('web_ceo_uploads') if isinstance(metadata, dict) else None
    items: list[dict[str, Any]] = []
    for raw in list(uploads or []):
        if not isinstance(raw, dict):
            continue
        path = str(raw.get('path') or '').strip()
        if not path:
            continue
        name = str(raw.get('name') or Path(path).name).strip() or Path(path).name or path
        mime_type = str(raw.get('mime_type') or raw.get('mimeType') or '').strip()
        kind = str(raw.get('kind') or '').strip() or _upload_kind(mime_type=mime_type, name=name)
        item = {
            'path': path,
            'name': name,
            'mime_type': mime_type,
            'kind': kind,
        }
        size = raw.get('size')
        if isinstance(size, (int, float)):
            item['size'] = int(size)
        items.append(item)
    if items:
        return items
    for raw in list(message.get('attachments') or []):
        path = str(raw or '').strip()
        if not path:
            continue
        name = Path(path).name or path
        mime_type = _guess_upload_mime_type(name)
        items.append(
            {
                'path': path,
                'name': name,
                'mime_type': mime_type,
                'kind': _upload_kind(mime_type=mime_type, name=name),
            }
        )
    return items


def _normalize_snapshot_tool_events(raw_events: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in list(raw_events or []):
        if not isinstance(raw, dict):
            continue
        status = str(raw.get('status') or '').strip().lower()
        if status not in {'running', 'success', 'error'}:
            status = 'error' if bool(raw.get('is_error')) else 'running' if bool(raw.get('is_update')) else 'success'
        item = {
            'status': status,
            'tool_name': str(raw.get('tool_name') or 'tool').strip() or 'tool',
            'text': str(raw.get('text') or '').strip(),
            'timestamp': str(raw.get('timestamp') or '').strip(),
            'tool_call_id': str(raw.get('tool_call_id') or '').strip(),
            'is_error': bool(raw.get('is_error')),
            'is_update': bool(raw.get('is_update')),
            'kind': str(raw.get('kind') or '').strip(),
            'source': str(raw.get('source') or '').strip().lower() or 'user',
        }
        elapsed_seconds = raw.get('elapsed_seconds')
        if isinstance(elapsed_seconds, (int, float)):
            item['elapsed_seconds'] = float(elapsed_seconds)
        items.append(item)
    return items


def _build_ceo_snapshot(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in list(messages or []):
        if not isinstance(raw, dict):
            continue
        if is_internal_ceo_user_message(raw):
            continue
        role = str(raw.get('role') or '').strip().lower()
        if role not in {'user', 'assistant', 'system'}:
            continue
        metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        content = _history_text(raw.get('content'))
        if role == 'user':
            raw_text = metadata.get('web_ceo_raw_text')
            if isinstance(raw_text, str) and raw_text.strip():
                content = raw_text.strip()
        attachments = _normalize_snapshot_attachments(raw) if role == 'user' else []
        execution_trace_summary = (
            dict(raw.get('execution_trace_summary'))
            if role == 'assistant' and isinstance(raw.get('execution_trace_summary'), dict)
            else {}
        )
        compression = (
            dict(raw.get('compression'))
            if role == 'assistant' and isinstance(raw.get('compression'), dict)
            else {}
        )
        legacy_tool_events = (
            _normalize_snapshot_tool_events(raw.get('tool_events'))
            if role == 'assistant'
            else []
        )
        if not content and not attachments and not execution_trace_summary and not compression and not legacy_tool_events:
            continue
        item = {'role': role, 'content': content}
        turn_id = str(raw.get('turn_id') or raw.get('metadata', {}).get('_transcript_turn_id') or '').strip() if isinstance(raw.get('metadata'), dict) else str(raw.get('turn_id') or '').strip()
        if turn_id:
            item['turn_id'] = turn_id
        timestamp = raw.get('timestamp')
        if isinstance(timestamp, str) and timestamp.strip():
            item['timestamp'] = timestamp.strip()
        if attachments:
            item['attachments'] = attachments
        if execution_trace_summary:
            item['execution_trace_summary'] = execution_trace_summary
        if compression:
            item['compression'] = compression
        if not execution_trace_summary and not compression and legacy_tool_events:
            item['tool_events'] = legacy_tool_events
        items.append(item)
    return items


def _resolve_final_execution_trace_summary(
    *,
    payload: dict[str, Any] | None,
    session: Any,
    persisted_session: Any,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    direct_summary = data.get("execution_trace_summary")
    if isinstance(direct_summary, dict) and direct_summary:
        return dict(direct_summary)
    snapshot_supplier = getattr(session, "_frontdoor_execution_trace_summary_snapshot", None)
    if callable(snapshot_supplier):
        try:
            summary = snapshot_supplier()
        except Exception:
            summary = None
        if isinstance(summary, dict) and summary:
            return dict(summary)
    return {}


def _should_forward_tool_event(*, session_id: str, event: AgentEvent) -> bool:
    _ = session_id
    if event.type not in {'tool_execution_start', 'tool_execution_update', 'tool_execution_end'}:
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    data = _coerce_event_data(payload)
    parent_session_id = str(data.get('parent_session_id') or '').strip()
    current_session_id = str(data.get('current_session_id') or '').strip()
    return not parent_session_id and not current_session_id


def _serialize_tool_event(event: AgentEvent) -> dict[str, Any] | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    data = _coerce_event_data(payload)
    tool_name = str(payload.get('tool_name') or 'tool').strip() or 'tool'
    text = str(payload.get('text') or '').strip()
    is_error = bool(payload.get('is_error'))
    source = str(payload.get('source') or data.get('source') or '').strip().lower() or 'user'
    if event.type == 'tool_execution_start':
        status = 'running'
        is_update = False
    elif event.type == 'tool_execution_update':
        status = 'running'
        is_update = True
    elif event.type == 'tool_execution_end':
        status = 'error' if is_error else 'success'
        is_update = False
    else:
        return None
    return {
        'status': status,
        'tool_name': tool_name or str(data.get('tool_name') or 'tool').strip() or 'tool',
        'text': text,
        'timestamp': event.timestamp,
        'tool_call_id': str(payload.get('tool_call_id') or data.get('tool_call_id') or ''),
        'is_error': is_error,
        'is_update': is_update,
        'kind': str(payload.get('kind') or '').strip(),
        'source': source,
    }


def _should_forward_message_end(payload: dict[str, Any] | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    if str(data.get("role") or "").strip().lower() != "assistant":
        return False
    if bool(data.get("heartbeat_internal")):
        return False
    text = str(data.get("text") or "").strip()
    if not text:
        return False
    if text == _HEARTBEAT_OK:
        return str(data.get("source") or "").strip().lower() == "cron"
    return True


def _is_internal_ack_message_end(payload: dict[str, Any] | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    if str(data.get("role") or "").strip().lower() != "assistant":
        return False
    text = str(data.get("text") or "").strip()
    if text != _HEARTBEAT_OK:
        return False
    return str(data.get("source") or "").strip().lower() in {"heartbeat", "cron"}


def _internal_ack_label(*, source: str, reason: str) -> str:
    normalized_source = str(source or "").strip().lower() or "heartbeat"
    normalized_reason = str(reason or "").strip() or "heartbeat_ok"
    suffix = "cron" if normalized_source == "cron" else "心跳"
    return f"已接收来自类型：{normalized_reason}的{suffix}"


@router.websocket('/ws/ceo')
async def ceo_websocket(websocket: WebSocket):
    await websocket.accept()
    if not get_bootstrap_security_service().is_unlocked():
        await websocket_send_json(
            websocket,
            build_envelope(channel='ceo', session_id='web:shared', type='error', data={'code': 'project_locked'}),
        )
        await websocket_close(websocket, code=4423)
        return
    try:
        agent = get_agent()
    except Exception as exc:
        if not is_no_ceo_model_configured_error(exc):
            raise
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='ceo',
                session_id='web:shared',
                type='error',
                data=no_ceo_model_configured_payload(),
            ),
        )
        await websocket_close(websocket, code=4503)
        return
    runtime_manager = get_runtime_manager(agent)
    transcript_store = getattr(agent, 'sessions', None)
    if transcript_store is None:
        try:
            await websocket_send_json(
                websocket,
                build_envelope(channel='ceo', session_id='web:shared', type='error', data={'code': 'session_manager_unavailable'}),
            )
            await websocket_close(websocket, code=4503)
        except WebSocketChannelClosed:
            return
        return
    state_store = WebCeoStateStore(workspace_path())
    requested_session_id = str(websocket.query_params.get('session_id') or '').strip()
    initial_catalog = build_ceo_session_catalog(
        transcript_store,
        active_session_id=requested_session_id,
        is_running_resolver=lambda key: _session_is_running(runtime_manager, key),
    )
    requested_item = find_ceo_session_catalog_item(initial_catalog, requested_session_id) if requested_session_id else None
    fallback_session_id = ''
    if requested_item is not None:
        session_id = str(requested_item.get('session_id') or requested_session_id).strip()
    elif requested_session_id:
        if str(requested_session_id or '').strip().startswith('web:'):
            if list(initial_catalog.get('items') or []):
                fallback_session_id = resolve_active_ceo_session_id(transcript_store, state_store)
                session_id = fallback_session_id
            else:
                create_web_ceo_session(transcript_store, session_id=requested_session_id)
                session_id = requested_session_id
        else:
            try:
                await websocket_send_json(
                    websocket,
                    build_envelope(channel='ceo', session_id=requested_session_id, type='error', data={'code': 'session_not_found'}),
                )
                await websocket_close(websocket, code=4404)
            except WebSocketChannelClosed:
                return
            return
    else:
        fallback_session_id = resolve_active_ceo_session_id(transcript_store, state_store)
        session_id = fallback_session_id
    session_path = transcript_store.get_path(session_id)
    is_channel_session = _is_channel_session_id(session_id)
    if is_channel_session:
        persisted_session = transcript_store.get_or_create(session_id) if session_path.exists() else None
    else:
        persisted_session = (
            create_web_ceo_session(transcript_store, session_id=session_id)
            if not session_path.exists()
            else transcript_store.get_or_create(session_id)
        )
        if ensure_ceo_session_metadata(persisted_session):
            transcript_store.save(persisted_session)
    state_store.set_active_session_id(session_id)
    memory_scope = dict(((getattr(persisted_session, 'metadata', None) or {}).get('memory_scope') or {}))
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        try:
            await websocket_send_json(
                websocket,
                build_envelope(channel='ceo', session_id=session_id, type='error', data={'code': 'task_service_unavailable'}),
            )
            await websocket_close(websocket, code=4503)
        except WebSocketChannelClosed:
            return
        return
    ensure_result = ensure_web_runtime_services(agent)
    if isawaitable(ensure_result):
        await ensure_result
    await _maybe_await(service.startup())
    queue = await _maybe_await(service.registry.subscribe_ceo(session_id))
    global_queue = await _maybe_await(service.registry.subscribe_global_ceo())
    stream_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    if ':' in session_id:
        default_channel, default_chat_id = session_id.split(':', 1)
    else:
        default_channel, default_chat_id = 'web', session_id
    session = runtime_manager.get_or_create(
        session_key=session_id,
        channel=default_channel or 'web',
        chat_id=default_chat_id or 'shared',
        memory_channel=(str(memory_scope.get('channel') or 'web') if not is_channel_session else None),
        memory_chat_id=(str(memory_scope.get('chat_id') or 'shared') if not is_channel_session else None),
    )
    persisted_messages = _build_ceo_snapshot(getattr(persisted_session, 'messages', []))
    current_turn_task: asyncio.Task[Any] | None = None
    closed = asyncio.Event()

    async def _safe_send(payload: dict[str, Any]) -> None:
        try:
            await websocket_send_json(websocket, payload)
        except WebSocketChannelClosed:
            closed.set()
            raise

    async def _push_stream_event(event_type: str, data: dict[str, Any] | None = None) -> None:
        try:
            await stream_queue.put(build_envelope(channel='ceo', session_id=session_id, type=event_type, data=data or {}))
        except RuntimeError:
            return

    async def _push_turn_patch() -> None:
        inflight_turn = _build_inflight_turn_snapshot(session, session_id)
        await _push_stream_event('ceo.turn.patch', {'inflight_turn': inflight_turn})

    def _current_session_is_running() -> bool:
        status = str(getattr(session.state, 'status', '') or '').strip().lower()
        return bool(getattr(session.state, 'is_running', False)) or status == 'running'

    def _register_turn_task(task: asyncio.Task[Any]) -> None:
        register_task = getattr(agent, '_register_active_task', None)
        if callable(register_task):
            register_task(session_id, task)

    def _clear_turn_task(task: asyncio.Task[Any]) -> None:
        nonlocal current_turn_task
        if current_turn_task is task:
            current_turn_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _run_user_turn(user_message: str | UserInputMessage | list[UserInputMessage]) -> None:
        try:
            if isinstance(user_message, list):
                if len(user_message) == 1:
                    await session.prompt(user_message[0])
                else:
                    prompt_batch = getattr(session, 'prompt_batch', None)
                    if callable(prompt_batch):
                        await prompt_batch(user_message)
                    else:
                        await session.prompt(user_message[-1])
            else:
                await session.prompt(user_message)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            snapshot = _build_inflight_turn_snapshot(session, session_id)
            await _push_stream_event(
                'ceo.error',
                {
                    'code': 'turn_failed',
                    'message': str(exc),
                    'source': str((snapshot or {}).get('source') or 'user').strip().lower() or 'user',
                    'turn_id': str((snapshot or {}).get('turn_id') or '').strip(),
                },
            )
            return

    async def sender(source_queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            payload = await source_queue.get()
            await _safe_send(payload)

    async def relay_session_event(event: AgentEvent) -> None:
        if event.type == 'frontdoor_interrupt':
            payload = dict(event.payload or {})
            await _push_stream_event(
                'ceo.turn.interrupt',
                {'interrupts': list(payload.get('interrupts') or [])},
            )
            await _push_turn_patch()
            return
        if event.type == 'control_ack':
            payload = dict(event.payload or {})
            await _push_stream_event('ceo.control_ack', payload)
            action = str(payload.get('action') or '').strip().lower()
            accepted = payload.get('accepted')
            should_push_patch = not (action == 'pause' and accepted is not False)
            if should_push_patch:
                await _push_turn_patch()
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                is_running=_current_session_is_running(),
            )
            return
        if event.type == 'state_snapshot':
            state = dict((event.payload or {}).get('state') or {})
            inflight_turn = _build_inflight_turn_snapshot(session, session_id)
            state_payload = {'state': state}
            if isinstance(inflight_turn, dict):
                source = str(inflight_turn.get('source') or '').strip().lower()
                turn_id = str(inflight_turn.get('turn_id') or '').strip()
                if source:
                    state_payload['source'] = source
                if turn_id:
                    state_payload['turn_id'] = turn_id
            await _push_stream_event('ceo.state', state_payload)
            status = str(state.get('status') or '').strip().lower()
            if status != 'paused':
                await _push_turn_patch()
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                is_running=bool(state.get('is_running')) or str(state.get('status') or '').strip().lower() == 'running',
            )
            return
        if event.type == 'message_end':
            payload = dict(event.payload or {})
            if not _should_forward_message_end(payload):
                return
            text = str(payload.get('text') or '').strip()
            source = str(payload.get('source') or 'user').strip().lower() or 'user'
            turn_id = str(payload.get('turn_id') or '').strip()
            persisted = transcript_store.get_or_create(session_id)
            execution_trace_summary = _resolve_final_execution_trace_summary(
                payload=payload,
                session=session,
                persisted_session=persisted,
            )
            if not turn_id:
                snapshot = None
                inflight_supplier = getattr(session, 'inflight_turn_snapshot', None)
                if callable(inflight_supplier):
                    try:
                        snapshot = inflight_supplier()
                    except Exception:
                        snapshot = None
                if isinstance(snapshot, dict):
                    turn_id = str(snapshot.get('turn_id') or '').strip()
            if _is_internal_ack_message_end(payload):
                reason = str(payload.get("heartbeat_reason") or "heartbeat_ok").strip() or "heartbeat_ok"
                await _push_stream_event(
                    'ceo.internal.ack',
                    {
                        'source': source if source in {'heartbeat', 'cron'} else 'heartbeat',
                        'reason': reason,
                        'label': _internal_ack_label(
                            source=source if source in {'heartbeat', 'cron'} else 'heartbeat',
                            reason=reason,
                        ),
                        'turn_id': turn_id,
                    },
                )
                return
            await _push_stream_event(
                'ceo.reply.final',
                {
                    'text': text,
                    'source': source,
                    'turn_id': turn_id,
                    **({'execution_trace_summary': execution_trace_summary} if execution_trace_summary else {}),
                },
            )
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                preview_text=text,
                message_count=len(list(getattr(persisted, 'messages', []) or [])),
                is_running=False,
            )
            return
        if not _should_forward_tool_event(session_id=session_id, event=event):
            return
        serialized = _serialize_tool_event(event)
        await _push_turn_patch()
        if serialized is not None:
            await _push_stream_event('ceo.agent.tool', serialized)

    unsubscribe = session.subscribe(relay_session_event)
    sender_task = asyncio.create_task(sender(queue))
    global_sender_task = asyncio.create_task(sender(global_queue))
    stream_task = asyncio.create_task(sender(stream_queue))
    try:
        inflight_turn = _build_inflight_turn_snapshot(session, session_id, persisted_session)
        await _safe_send(build_envelope(channel='ceo', session_id=session_id, type='hello', data={'session_id': session_id}))
        await _safe_send(
            build_envelope(
                channel='ceo',
                session_id=session_id,
                type='snapshot.ceo',
                data={'messages': persisted_messages, 'inflight_turn': inflight_turn},
            )
        )
        await _safe_send(build_envelope(channel='ceo', session_id=session_id, type='ceo.state', data={'state': session.state_dict()}))
        initial_catalog = build_ceo_session_catalog(
            transcript_store,
            active_session_id=resolve_active_ceo_session_id(transcript_store, state_store),
            is_running_resolver=lambda key: _session_is_running(runtime_manager, key),
        )
        await _safe_send(
            build_envelope(
                channel='ceo',
                session_id=session_id,
                type='ceo.sessions.snapshot',
                data={
                    'items': initial_catalog.get('items', []),
                    'channel_groups': initial_catalog.get('channel_groups', []),
                    'active_session_id': initial_catalog.get('active_session_id') or session_id,
                    'active_session_family': initial_catalog.get('active_session_family') or ceo_session_family(initial_catalog.get('active_session_id') or session_id),
                },
            )
        )
        while True:
            if closed.is_set():
                break
            data = await websocket_receive_json(websocket)
            message_type = str(data.get('type') or '')
            if message_type == 'client.resume_interrupt':
                if _current_session_is_running() or (current_turn_task is not None and not current_turn_task.done()):
                    await _safe_send(
                        build_envelope(
                            channel='ceo',
                            session_id=session_id,
                            type='error',
                            data={'code': 'ceo_turn_in_progress'},
                        )
                    )
                    continue
                current_turn_task = asyncio.create_task(
                    session.resume_frontdoor_interrupt(resume_value=data.get('resume'))
                )
                _register_turn_task(current_turn_task)
                current_turn_task.add_done_callback(_clear_turn_task)
                continue
            if message_type == 'client.pause_turn':
                if _current_session_is_running():
                    await session.pause(manual=True)
                else:
                    await _push_stream_event('ceo.control_ack', {'action': 'pause', 'accepted': False, 'reason': 'no_active_turn'})
                continue
            if message_type != 'client.user_message':
                continue
            if is_channel_session:
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={
                            'code': 'channel_session_readonly',
                            'message': '渠道会话为只读，仅供查看渠道历史消息。',
                        },
                    )
                )
                continue
            try:
                user_messages = _normalize_client_user_messages(session_id, data)
            except HTTPException as exc:
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={'code': str(exc.detail or 'invalid_upload'), 'status_code': exc.status_code},
                    )
                )
                continue
            if not user_messages:
                continue
            if bool(getattr(session, "has_blocking_tool_execution", lambda: False)()):
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={
                            'code': 'ceo_blocked_by_running_tool',
                            'message': '当前会话仍在等待长工具结束，暂不接收新的用户输入。',
                        },
                    )
                )
                continue
            if _current_session_is_running() or (current_turn_task is not None and not current_turn_task.done()):
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={'code': 'ceo_turn_in_progress'},
                    )
                )
                continue
            persisted = transcript_store.get_or_create(session_id)
            preview_text = _history_text(user_messages[-1].content)
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                preview_text=preview_text,
                message_count=len(list(getattr(persisted, 'messages', []) or [])) + len(user_messages),
                is_running=True,
            )
            current_turn_task = asyncio.create_task(_run_user_turn(user_messages))
            _register_turn_task(current_turn_task)
            current_turn_task.add_done_callback(_clear_turn_task)
    except (WebSocketDisconnect, WebSocketChannelClosed):
        pass
    finally:
        unsubscribe()
        sender_task.cancel()
        global_sender_task.cancel()
        stream_task.cancel()
        await asyncio.gather(sender_task, global_sender_task, stream_task, return_exceptions=True)
        await _maybe_await(service.registry.unsubscribe_ceo(session_id, queue))
        await _maybe_await(service.registry.unsubscribe_global_ceo(global_queue))
