from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import mimetypes
import shutil
import uuid
from inspect import isawaitable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

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
    transcript_messages,
    WEB_CEO_IMAGE_UPLOAD_MAX_BYTES,
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
_APPROVAL_INTERRUPT_KINDS = {
    "frontdoor_tool_approval",
    "frontdoor_tool_approval_batch",
}


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


def _approval_interrupts(items: Any) -> list[dict[str, Any]]:
    approvals: list[dict[str, Any]] = []
    for raw in list(items or []):
        if not isinstance(raw, dict):
            continue
        value = raw.get("value") if isinstance(raw.get("value"), dict) else {}
        kind = str(value.get("kind") or "").strip()
        if kind in _APPROVAL_INTERRUPT_KINDS:
            approvals.append(dict(raw))
    return approvals


def _pending_tool_approval_interrupts(
    session: Any,
    session_id: str,
    persisted_session: Any | None = None,
) -> list[dict[str, Any]]:
    _ = session_id
    state = getattr(session, "state", None)
    from_state = _approval_interrupts(getattr(state, "pending_interrupts", None))
    if from_state:
        return from_state
    status = str(getattr(state, "status", "") or "").strip().lower()
    is_running = bool(getattr(state, "is_running", False)) or status == "running"
    if is_running:
        return []
    try:
        snapshot, _snapshot_source = resolve_execution_snapshot(session, persisted_session)
    except Exception:
        snapshot = None
    if isinstance(snapshot, dict):
        from_snapshot = _approval_interrupts(snapshot.get("interrupts"))
        if from_snapshot:
            return from_snapshot
    return []


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


def _image_upload_too_large_error(*, name: str, size: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            'code': 'image_upload_too_large',
            'name': str(name or '').strip() or 'image',
            'size_bytes': int(size or 0),
            'limit_bytes': WEB_CEO_IMAGE_UPLOAD_MAX_BYTES,
            'message': f'Image upload exceeds the 5 MiB limit: {name}',
        },
    )


def _validate_uploaded_descriptor_limits(item: dict[str, Any]) -> None:
    if str(item.get('kind') or '').strip().lower() != 'image':
        return
    size = int(item.get('size') or 0)
    if size > WEB_CEO_IMAGE_UPLOAD_MAX_BYTES:
        raise _image_upload_too_large_error(name=str(item.get('name') or 'image'), size=size)


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
    item = _serialize_upload_descriptor(
        target_path,
        name=original_name,
        mime_type=_guess_upload_mime_type(original_name, getattr(upload, 'content_type', None)),
    )
    try:
        _validate_uploaded_descriptor_limits(item)
    except HTTPException:
        target_path.unlink(missing_ok=True)
        raise
    return item


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


def _build_user_message(text: str, uploads: list[dict[str, Any]]) -> str | UserInputMessage:
    if not uploads:
        return text

    text_value = str(text or '')
    note = _uploaded_files_note(uploads)
    merged_text = f"{text_value}\n\n{note}" if (note and text_value) else (note or text_value)
    return UserInputMessage(
        content=merged_text or note or text_value,
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


def _build_preserved_turn_snapshot(
    session: Any,
    session_id: str,
    persisted_session: Any | None = None,
) -> dict[str, Any] | None:
    snapshot: dict[str, Any] | None = None
    getter = getattr(session, "preserved_inflight_turn_snapshot", None)
    if callable(getter):
        try:
            snapshot = getter()
        except Exception:
            snapshot = None
    if not isinstance(snapshot, dict):
        return None
    preserved_turn_id = str(snapshot.get("turn_id") or "").strip()
    if preserved_turn_id and _assistant_turn_already_persisted(persisted_session, turn_id=preserved_turn_id):
        return None
    current_snapshot = _build_inflight_turn_snapshot(session, session_id)
    current_turn_id = str((current_snapshot or {}).get("turn_id") or "").strip()
    current_source = str((current_snapshot or {}).get("source") or "").strip().lower()
    preserved_source = str(snapshot.get("source") or "").strip().lower()
    if preserved_turn_id and current_turn_id and preserved_turn_id == current_turn_id:
        if not preserved_source or not current_source or preserved_source == current_source:
            return None
    return snapshot


def _build_live_turn_payload(
    session: Any,
    session_id: str,
    persisted_session: Any | None = None,
) -> dict[str, Any]:
    baseline_context = _latest_persisted_assistant_canonical_context(persisted_session)
    inflight_turn = _with_canonical_context_delta(
        _build_inflight_turn_snapshot(session, session_id, persisted_session),
        baseline_context,
    )
    preserved_turn = _with_canonical_context_delta(
        _build_preserved_turn_snapshot(session, session_id, persisted_session),
        baseline_context,
    )
    payload: dict[str, Any] = {"inflight_turn": inflight_turn}
    if preserved_turn is not None:
        payload["preserved_turn"] = preserved_turn
    return payload


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


@router.get('/ceo/uploads/file')
async def get_ceo_uploaded_file(
    session_id: str = Query('web:shared'),
    path: str = Query(...),
):
    candidate = _resolve_uploaded_file(session_id, path)
    name = safe_filename(candidate.name) or candidate.name or 'attachment'
    return FileResponse(
        str(candidate),
        media_type=_guess_upload_mime_type(name),
        filename=name,
        content_disposition_type='inline',
    )


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


def _snapshot_message_transcript_state(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return str(metadata.get("_transcript_state") or "").strip().lower()


def _build_ceo_snapshot(
    messages: list[dict[str, Any]] | None,
    *,
    inflight_turn: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    inflight_payload = inflight_turn if isinstance(inflight_turn, dict) else {}
    inflight_status = str(inflight_payload.get("status") or "").strip().lower()
    hide_pending_users = inflight_status in {"running", "in_progress", "active"}
    items: list[dict[str, Any]] = []
    previous_assistant_context: dict[str, Any] = {}
    for raw in list(messages or []):
        if not isinstance(raw, dict):
            continue
        metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        if metadata.get('ui_visible') is False:
            continue
        if is_internal_ceo_user_message(raw):
            continue
        role = str(raw.get('role') or '').strip().lower()
        if role not in {'user', 'assistant', 'system'}:
            continue
        if hide_pending_users and role == "user" and _snapshot_message_transcript_state(raw) == "pending":
            continue
        content = _history_text(raw.get('content'))
        if role == 'user':
            raw_text = metadata.get('web_ceo_raw_text')
            if isinstance(raw_text, str) and isinstance(metadata.get('web_ceo_uploads'), list):
                content = raw_text
        attachments = _normalize_snapshot_attachments(raw) if role == 'user' else []
        canonical_context = (
            dict(raw.get('canonical_context'))
            if role == 'assistant' and isinstance(raw.get('canonical_context'), dict)
            else {}
        )
        compression = (
            dict(raw.get('compression'))
            if role == 'assistant' and isinstance(raw.get('compression'), dict)
            else {}
        )
        if not content and not attachments and not canonical_context and not compression:
            continue
        item = {'role': role, 'content': content}
        if role == 'assistant':
            status = str(raw.get('status') or '').strip().lower()
            if status:
                item['status'] = status
        turn_id = str(raw.get('turn_id') or raw.get('metadata', {}).get('_transcript_turn_id') or '').strip() if isinstance(raw.get('metadata'), dict) else str(raw.get('turn_id') or '').strip()
        if turn_id:
            item['turn_id'] = turn_id
        timestamp = raw.get('timestamp')
        if isinstance(timestamp, str) and timestamp.strip():
            item['timestamp'] = timestamp.strip()
        if attachments:
            item['attachments'] = attachments
        if canonical_context:
            item['canonical_context'] = canonical_context
            item['canonical_context_delta'] = _canonical_context_delta(previous_assistant_context, canonical_context)
            previous_assistant_context = canonical_context
        if compression:
            item['compression'] = compression
        items.append(item)
    return items


def _snapshot_message_turn_id(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    direct = str(message.get("turn_id") or "").strip()
    if direct:
        return direct
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return str(metadata.get("_transcript_turn_id") or "").strip()


def _assistant_turn_already_persisted(persisted_session: Any | None, *, turn_id: str) -> bool:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return False
    persisted_messages = getattr(persisted_session, "messages", None)
    for raw in reversed(list(persisted_messages or [])):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("role") or "").strip().lower() != "assistant":
            continue
        if _snapshot_message_turn_id(raw) != normalized_turn_id:
            continue
        return True
    return False


def _canonical_context_copy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return copy.deepcopy(raw)


def _canonical_value_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_stage_identity(stage: dict[str, Any], index: int) -> str:
    return str(stage.get("stage_id") or stage.get("stage_index") or f"stage:{index}").strip()


def _canonical_round_identity(round_payload: dict[str, Any], index: int) -> str:
    return str(round_payload.get("round_id") or round_payload.get("round_index") or f"round:{index}").strip()


def _canonical_tool_identity(tool_payload: dict[str, Any], index: int) -> str:
    tool_call_id = str(tool_payload.get("tool_call_id") or "").strip()
    if tool_call_id:
        return tool_call_id
    tool_name = str(tool_payload.get("tool_name") or "tool").strip() or "tool"
    return f"{tool_name}:{index}"


def _canonical_context_delta(previous_context: Any, current_context: Any) -> dict[str, Any]:
    previous = _canonical_context_copy(previous_context)
    current = _canonical_context_copy(current_context)
    current_stages = [dict(item) for item in list(current.get("stages") or []) if isinstance(item, dict)]
    if not current_stages:
        return {}
    previous_stages = {
        _canonical_stage_identity(stage, index): dict(stage)
        for index, stage in enumerate(list(previous.get("stages") or []))
        if isinstance(stage, dict)
    }
    delta_stages: list[dict[str, Any]] = []
    for stage_index, stage in enumerate(current_stages):
        stage_identity = _canonical_stage_identity(stage, stage_index)
        previous_stage = previous_stages.get(stage_identity)
        if previous_stage is None:
            delta_stages.append(copy.deepcopy(stage))
            continue
        stage_header = {key: copy.deepcopy(value) for key, value in stage.items() if key != "rounds"}
        previous_stage_header = {
            key: copy.deepcopy(value) for key, value in previous_stage.items() if key != "rounds"
        }
        stage_header_changed = _canonical_value_fingerprint(previous_stage_header) != _canonical_value_fingerprint(stage_header)
        previous_rounds = {
            _canonical_round_identity(round_payload, round_index): dict(round_payload)
            for round_index, round_payload in enumerate(list(previous_stage.get("rounds") or []))
            if isinstance(round_payload, dict)
        }
        delta_rounds: list[dict[str, Any]] = []
        for round_index, round_payload in enumerate(list(stage.get("rounds") or [])):
            if not isinstance(round_payload, dict):
                continue
            round_identity = _canonical_round_identity(round_payload, round_index)
            previous_round = previous_rounds.get(round_identity)
            if previous_round is None:
                delta_rounds.append(copy.deepcopy(round_payload))
                continue
            previous_tools = {
                _canonical_tool_identity(tool_payload, tool_index): dict(tool_payload)
                for tool_index, tool_payload in enumerate(list(previous_round.get("tools") or []))
                if isinstance(tool_payload, dict)
            }
            delta_tools: list[dict[str, Any]] = []
            for tool_index, tool_payload in enumerate(list(round_payload.get("tools") or [])):
                if not isinstance(tool_payload, dict):
                    continue
                tool_identity = _canonical_tool_identity(tool_payload, tool_index)
                previous_tool = previous_tools.get(tool_identity)
                if previous_tool is None:
                    delta_tools.append(copy.deepcopy(tool_payload))
                    continue
                if _canonical_value_fingerprint(previous_tool) != _canonical_value_fingerprint(tool_payload):
                    delta_tools.append(copy.deepcopy(tool_payload))
            if delta_tools:
                delta_round = copy.deepcopy(round_payload)
                delta_round["tools"] = delta_tools
                delta_rounds.append(delta_round)
        if stage_header_changed or delta_rounds:
            delta_stage = copy.deepcopy(stage)
            delta_stage["rounds"] = delta_rounds
            delta_stages.append(delta_stage)
    if not delta_stages:
        return {}
    delta: dict[str, Any] = {"stages": delta_stages}
    active_stage_id = str(current.get("active_stage_id") or "").strip()
    if active_stage_id and any(str(stage.get("stage_id") or "").strip() == active_stage_id for stage in delta_stages):
        delta["active_stage_id"] = active_stage_id
    if current.get("transition_required") is True:
        delta["transition_required"] = True
    return delta


def _assistant_canonical_context(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    if str(message.get("role") or "").strip().lower() != "assistant":
        return {}
    return _canonical_context_copy(message.get("canonical_context"))


def _latest_persisted_assistant_canonical_context(persisted_session: Any | None) -> dict[str, Any]:
    persisted_messages = getattr(persisted_session, "messages", None)
    for raw in reversed(list(persisted_messages or [])):
        canonical_context = _assistant_canonical_context(raw)
        if canonical_context:
            return canonical_context
    return {}


def _with_canonical_context_delta(payload: dict[str, Any] | None, previous_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    next_payload = copy.deepcopy(payload)
    canonical_context = _canonical_context_copy(next_payload.get("canonical_context"))
    if not canonical_context:
        next_payload.pop("canonical_context_delta", None)
        return next_payload
    delta = _canonical_context_delta(previous_context, canonical_context)
    if delta:
        next_payload["canonical_context_delta"] = delta
    else:
        next_payload.pop("canonical_context_delta", None)
    return next_payload


def _resolve_final_canonical_context(
    *,
    payload: dict[str, Any] | None,
    session: Any,
    persisted_session: Any,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    direct_context = data.get("canonical_context")
    if isinstance(direct_context, dict) and direct_context:
        return dict(direct_context)
    snapshot_supplier = getattr(session, "_frontdoor_visible_canonical_context_snapshot", None)
    if callable(snapshot_supplier):
        try:
            canonical_context = snapshot_supplier()
        except Exception:
            canonical_context = None
        if isinstance(canonical_context, dict) and canonical_context:
            return dict(canonical_context)
    return {}


def _resolve_final_canonical_context_delta(
    *,
    payload: dict[str, Any] | None,
    session: Any,
    persisted_session: Any,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    direct_delta = data.get("canonical_context_delta")
    if isinstance(direct_delta, dict) and direct_delta:
        return dict(direct_delta)
    canonical_context = _resolve_final_canonical_context(
        payload=payload,
        session=session,
        persisted_session=persisted_session,
    )
    return _canonical_context_delta(
        _latest_persisted_assistant_canonical_context(persisted_session),
        canonical_context,
    )


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
    text = str(
        payload.get('text')
        or data.get('text')
        or data.get('output_text')
        or data.get('output_preview_text')
        or ''
    ).strip()
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
        'output_text': str(data.get('output_text') or '').strip(),
        'output_preview_text': str(data.get('output_preview_text') or '').strip(),
        'arguments_text': str(data.get('arguments_text') or '').strip(),
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
    text = str(data.get("text") or "").strip()
    if not text:
        return False
    if bool(data.get("heartbeat_internal")) and text != _HEARTBEAT_OK:
        return True
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
    source = str(data.get("source") or "").strip().lower()
    if source == "heartbeat" and str(data.get("heartbeat_reason") or "").strip().lower() == "task_terminal":
        return False
    return source in {"heartbeat", "cron"}


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
    turn_payload = _build_live_turn_payload(session, session_id, persisted_session)
    persisted_messages = _build_ceo_snapshot(
        getattr(persisted_session, 'messages', []),
        inflight_turn=turn_payload.get("inflight_turn") if isinstance(turn_payload, dict) else None,
    )
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
        try:
            persisted_session = transcript_store.get_or_create(session_id)
        except Exception:
            persisted_session = None
        await _push_stream_event('ceo.turn.patch', _build_live_turn_payload(session, session_id, persisted_session))

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

    async def _invoke_user_turn(user_message: str | UserInputMessage | list[UserInputMessage]) -> None:
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

    async def _drain_queued_follow_ups() -> list[UserInputMessage]:
        drain_follow_ups = getattr(session, 'drain_queued_follow_up_messages', None)
        if not callable(drain_follow_ups):
            return []
        drained = await _maybe_await(drain_follow_ups())
        return [
            item
            for item in list(drained or [])
            if isinstance(item, UserInputMessage)
        ]

    async def _run_user_turn(user_message: str | UserInputMessage | list[UserInputMessage]) -> None:
        current_payload = user_message
        try:
            while True:
                await _invoke_user_turn(current_payload)
                queued_follow_ups = await _drain_queued_follow_ups()
                if not queued_follow_ups:
                    break
                archive_follow_up_chain_transition = getattr(session, 'archive_follow_up_chain_transition', None)
                if callable(archive_follow_up_chain_transition):
                    follow_up_turn_ids = {
                        str((getattr(item, 'metadata', None) or {}).get('_transcript_turn_id') or '').strip()
                        for item in list(queued_follow_ups or [])
                        if isinstance(item, UserInputMessage)
                    }
                    await _maybe_await(
                        archive_follow_up_chain_transition(
                            pending_follow_up_turn_ids=follow_up_turn_ids,
                        )
                    )
                current_payload = list(queued_follow_ups)
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
        if event.type == 'assistant_stream_delta':
            payload = dict(event.payload or {})
            await _push_stream_event(
                'ceo.reply.delta',
                {
                    'turn_id': str(payload.get('turn_id') or '').strip(),
                    'source': str(payload.get('source') or 'user').strip().lower() or 'user',
                    'text': str(payload.get('text') or ''),
                    'seq': int(payload.get('seq') or 0),
                },
            )
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
            snapshot = _build_inflight_turn_snapshot(session, session_id)
            user_messages = [
                dict(item)
                for item in list((snapshot or {}).get('user_messages') or [])
                if isinstance(item, dict)
            ]
            persisted = transcript_store.get_or_create(session_id)
            canonical_context = _resolve_final_canonical_context(
                payload=payload,
                session=session,
                persisted_session=persisted,
            )
            canonical_context_delta = _resolve_final_canonical_context_delta(
                payload=payload,
                session=session,
                persisted_session=persisted,
            )
            if not turn_id:
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
                    **({'user_messages': user_messages} if user_messages else {}),
                    **({'canonical_context': canonical_context} if canonical_context else {}),
                    **({'canonical_context_delta': canonical_context_delta} if canonical_context_delta else {}),
                },
            )
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                preview_text=text,
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
        await _safe_send(build_envelope(channel='ceo', session_id=session_id, type='hello', data={'session_id': session_id}))
        await _safe_send(
            build_envelope(
                channel='ceo',
                session_id=session_id,
                type='snapshot.ceo',
                data={'messages': persisted_messages, **turn_payload},
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
            persisted = transcript_store.get_or_create(session_id)
            if _pending_tool_approval_interrupts(session, session_id, persisted):
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={
                            'code': 'ceo_approval_pending',
                            'message': 'A CEO tool approval batch is pending. Complete approval before sending a new message.',
                        },
                    )
                )
                continue
            if _current_session_is_running() or (current_turn_task is not None and not current_turn_task.done()):
                queue_follow_up_batch = getattr(session, 'queue_follow_up_batch', None)
                if not callable(queue_follow_up_batch):
                    await _safe_send(
                        build_envelope(
                            channel='ceo',
                            session_id=session_id,
                            type='error',
                            data={'code': 'ceo_turn_in_progress'},
                        )
                    )
                    continue
                try:
                    await _maybe_await(queue_follow_up_batch(user_messages, persist_transcript=True))
                except Exception as exc:
                    await _safe_send(
                        build_envelope(
                            channel='ceo',
                            session_id=session_id,
                            type='error',
                            data={
                                'code': 'ceo_follow_up_enqueue_failed',
                                'message': str(exc),
                            },
                        )
                    )
                    continue
                preview_text = _history_text(user_messages[-1].content)
                _publish_ceo_session_patch(
                    agent=agent,
                    transcript_store=transcript_store,
                    runtime_manager=runtime_manager,
                    state_store=state_store,
                    session_id=session_id,
                    preview_text=preview_text,
                    message_count=len(transcript_messages(persisted)),
                    is_running=True,
                )
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
            preview_text = _history_text(user_messages[-1].content)
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                preview_text=preview_text,
                message_count=len(transcript_messages(persisted)) + len(user_messages),
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
