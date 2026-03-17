from __future__ import annotations

import asyncio
import base64
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect

from g3ku.core.messages import UserInputMessage
from g3ku.core.events import AgentEvent
from g3ku.runtime.legacy_metadata import is_legacy_runtime_metadata_message
from g3ku.runtime.web_ceo_sessions import (
    WebCeoStateStore,
    create_web_ceo_session,
    ensure_active_web_ceo_session,
    ensure_ceo_session_metadata,
    upload_dir_for_session,
    workspace_path,
)
from g3ku.shells.web import ensure_web_runtime_services, get_agent, get_runtime_manager
from g3ku.utils.helpers import safe_filename
from main.protocol import build_envelope

router = APIRouter()


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


def _build_inflight_turn_snapshot(session: Any) -> dict[str, Any] | None:
    getter = getattr(session, 'inflight_turn_snapshot', None)
    if not callable(getter):
        return None
    snapshot = getter()
    return snapshot if isinstance(snapshot, dict) else None


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


def _build_ceo_snapshot(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in list(messages or []):
        if not isinstance(raw, dict):
            continue
        if is_legacy_runtime_metadata_message(raw):
            continue
        role = str(raw.get('role') or '').strip().lower()
        if role not in {'user', 'assistant', 'system'}:
            continue
        content = _history_text(raw.get('content'))
        if not content:
            continue
        item = {'role': role, 'content': content}
        timestamp = raw.get('timestamp')
        if isinstance(timestamp, str) and timestamp.strip():
            item['timestamp'] = timestamp.strip()
        items.append(item)
    return items


def _should_forward_tool_event(*, session_id: str, event: AgentEvent) -> bool:
    _ = session_id
    if event.type not in {'tool_execution_start', 'tool_execution_end'}:
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    data = _coerce_event_data(payload)
    parent_session_id = str(data.get('parent_session_id') or '').strip()
    current_session_id = str(data.get('current_session_id') or '').strip()
    return not parent_session_id and not current_session_id


def _serialize_tool_event(event: AgentEvent) -> dict[str, Any] | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    tool_name = str(payload.get('tool_name') or 'tool').strip() or 'tool'
    text = str(payload.get('text') or '').strip()
    is_error = bool(payload.get('is_error'))
    if event.type == 'tool_execution_start':
        status = 'running'
    elif event.type == 'tool_execution_end':
        status = 'error' if is_error else 'success'
    else:
        return None
    return {
        'status': status,
        'tool_name': tool_name,
        'text': text,
        'timestamp': event.timestamp,
        'tool_call_id': str(payload.get('tool_call_id') or ''),
        'is_error': is_error,
    }


@router.websocket('/ws/ceo')
async def ceo_websocket(websocket: WebSocket):
    await websocket.accept()
    agent = get_agent()
    runtime_manager = get_runtime_manager(agent)
    transcript_store = getattr(agent, 'sessions', None)
    if transcript_store is None:
        await websocket.send_json(build_envelope(channel='ceo', session_id='web:shared', type='error', data={'code': 'session_manager_unavailable'}))
        await websocket.close(code=4503)
        return
    state_store = WebCeoStateStore(workspace_path())
    requested_session_id = str(websocket.query_params.get('session_id') or '').strip()
    session_id = requested_session_id or ensure_active_web_ceo_session(transcript_store, state_store)
    session_path = transcript_store.get_path(session_id)
    persisted_session = (
        create_web_ceo_session(transcript_store, session_id=session_id)
        if not session_path.exists()
        else transcript_store.get_or_create(session_id)
    )
    if ensure_ceo_session_metadata(persisted_session):
        transcript_store.save(persisted_session)
    state_store.set_active_session_id(session_id)
    memory_scope = dict((persisted_session.metadata or {}).get('memory_scope') or {})
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        await websocket.send_json(build_envelope(channel='ceo', session_id=session_id, type='error', data={'code': 'task_service_unavailable'}))
        await websocket.close(code=4503)
        return
    await ensure_web_runtime_services(agent)
    await service.startup()
    queue = await service.registry.subscribe_ceo(session_id)
    global_queue = await service.registry.subscribe_global_ceo()
    stream_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    if ':' in session_id:
        default_channel, default_chat_id = session_id.split(':', 1)
    else:
        default_channel, default_chat_id = 'web', session_id
    session = runtime_manager.get_or_create(
        session_key=session_id,
        channel=default_channel or 'web',
        chat_id=default_chat_id or 'shared',
        memory_channel=str(memory_scope.get('channel') or 'web'),
        memory_chat_id=str(memory_scope.get('chat_id') or 'shared'),
    )
    persisted_messages = _build_ceo_snapshot(getattr(persisted_session, 'messages', []))
    current_turn_task: asyncio.Task[Any] | None = None

    async def _safe_send(payload: dict[str, Any]) -> None:
        await websocket.send_json(payload)

    async def _push_stream_event(event_type: str, data: dict[str, Any] | None = None) -> None:
        try:
            await stream_queue.put(build_envelope(channel='ceo', session_id=session_id, type=event_type, data=data or {}))
        except RuntimeError:
            return

    def _session_is_running() -> bool:
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

    async def _run_user_turn(user_message: str | UserInputMessage) -> None:
        try:
            result = await session.prompt(user_message)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await _push_stream_event('ceo.error', {'code': 'turn_failed', 'message': str(exc)})
            return
        await _push_stream_event('ceo.reply.final', {'text': str(result.output or '')})

    async def sender(source_queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            payload = await source_queue.get()
            await _safe_send(payload)

    async def relay_session_event(event: AgentEvent) -> None:
        if event.type == 'control_ack':
            payload = dict(event.payload or {})
            await _push_stream_event('ceo.control_ack', payload)
            return
        if event.type == 'state_snapshot':
            state = dict((event.payload or {}).get('state') or {})
            await _push_stream_event('ceo.state', {'state': state})
            return
        if not _should_forward_tool_event(session_id=session_id, event=event):
            return
        serialized = _serialize_tool_event(event)
        if serialized is None:
            return
        await _push_stream_event('ceo.agent.tool', serialized)

    unsubscribe = session.subscribe(relay_session_event)
    sender_task = asyncio.create_task(sender(queue))
    global_sender_task = asyncio.create_task(sender(global_queue))
    stream_task = asyncio.create_task(sender(stream_queue))
    try:
        inflight_turn = _build_inflight_turn_snapshot(session)
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
        while True:
            data = await websocket.receive_json()
            message_type = str(data.get('type') or '')
            if message_type == 'client.pause_turn':
                if _session_is_running():
                    await session.pause()
                else:
                    await _push_stream_event('ceo.control_ack', {'action': 'pause', 'accepted': False, 'reason': 'no_active_turn'})
                continue
            if message_type != 'client.user_message':
                continue
            text = str(data.get('text') or '')
            try:
                uploads = _normalize_uploaded_files(session_id, data.get('uploads'))
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
            if not text.strip() and not uploads:
                continue
            if _session_is_running() or (current_turn_task is not None and not current_turn_task.done()):
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={'code': 'ceo_turn_in_progress'},
                    )
                )
                continue
            user_message = _build_user_message(text, uploads)
            current_turn_task = asyncio.create_task(_run_user_turn(user_message))
            _register_turn_task(current_turn_task)
            current_turn_task.add_done_callback(_clear_turn_task)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        sender_task.cancel()
        global_sender_task.cancel()
        stream_task.cancel()
        await asyncio.gather(sender_task, global_sender_task, stream_task, return_exceptions=True)
        await service.registry.unsubscribe_ceo(session_id, queue)
        await service.registry.unsubscribe_global_ceo(global_queue)
