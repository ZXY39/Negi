from __future__ import annotations

import asyncio
import copy
import mimetypes
import shutil
import uuid
from inspect import isawaitable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from loguru import logger

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.deployment.data_root import data_root
from g3ku.runtime.api.ceo_media import rewrite_assistant_media_content
from g3ku.runtime.api.external_turns import get_external_turn_service
from g3ku.runtime.ceo_catalog_offload import (
    build_ceo_session_catalog_async,
    build_ceo_session_catalog_cached,
    run_off_event_loop,
)
from g3ku.runtime.external_sessions import ExternalSessionEntry, get_external_session_registry
from g3ku.runtime.frontdoor.canonical_context import (
    TRANSCRIPT_PROJECTION_MODE,
)
from g3ku.runtime.frontdoor.canonical_context import (
    apply_cc_upsert as _apply_cc_upsert,
)
from g3ku.runtime.frontdoor.canonical_context import (
    materialize_transcript_view as _materialize_transcript_view,
)
from g3ku.runtime.frontdoor.canonical_context import (
    project_canonical_context_for_transcript as _project_canonical_context_for_transcript,
)
from g3ku.runtime.frontdoor.canonical_context import (
    ui_canonical_context_delta as _ui_canonical_context_delta,
)
from g3ku.runtime.frontdoor.canonical_context import (
    ui_canonical_context_delta_from_views as _ui_canonical_context_delta_from_views,
)
from g3ku.runtime.session_keys import (
    EXTERNAL_SESSION_KEY_PREFIX,
    is_channel_session_key,
)
from g3ku.runtime.web_ceo_sessions import (
    EXTERNAL_UPLOAD_ROOT,
    WEB_CEO_IMAGE_UPLOAD_MAX_BYTES,
    WEB_CEO_VOICE_UPLOAD_MAX_BYTES,
    WebCeoStateStore,
    build_channel_ceo_session_item,
    build_local_ceo_session_item,
    build_session_summary,
    ceo_session_family,
    create_web_ceo_session,
    ensure_ceo_session_metadata,
    final_reply_canonical_merge,
    find_ceo_session_catalog_item,
    is_internal_ceo_user_message,
    read_inflight_turn_snapshot,
    read_session_turn_token_usage,
    resolve_active_ceo_session_id,
    resolve_execution_snapshot,
    transcript_messages,
    upload_dir_for_session,
    workspace_path,
)
from g3ku.security import get_bootstrap_security_service
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
# 连续几帧都写不出去才认定这条 socket 已废（单帧失败通常是载荷问题，下一帧还能发）。
_SENDER_CONSECUTIVE_FAILURE_LIMIT = 3
# live 轨道补丁的合并窗口：一次工具事件就重建整份工作集（实测 100-270ms 事件循环
# CPU），一回合几十次会把帧挤到回合末尾才出得去。窗口末按当前状态重建一帧即可，
# 补丁里的 delta 始终相对最后落库的 assistant 行，丢掉中间帧不丢阶段；回合结束帧
# （final / 静默回执 / 错误）前强制冲一帧，快回合也不会丢轨道。
_CEO_TURN_PATCH_MIN_INTERVAL_S = 0.25
# 静默回合的历史占位文案：存量转录里仍带这一行，快照层按静默归一化后不显示到会话框。
_LEGACY_SILENT_REPLY_TEXT = "信息已静默"
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
    return is_channel_session_key(session_id)


def _external_entry_for_channel_input(session_id: str) -> ExternalSessionEntry | None:
    """网页可往这个渠道会话里投消息所需的注册表条目，拿不到就保持只读。

    判定不是新策略而是依赖：提交走 ``ExternalTurnService.submit``，它的必填参数就是
    这条 ``ExternalSessionEntry``（``bridge_id`` + ``external_key`` 是回复出站的路由）。
    所以 ``china:*`` 归档与未注册的 ``ext:`` 天然拿不到条目，无需额外的开关位。
    """
    if not str(session_id or "").startswith(EXTERNAL_SESSION_KEY_PREFIX):
        return None
    try:
        return get_external_session_registry().get_by_session_key(session_id)
    except Exception:
        logger.debug("external registry lookup failed for {}", session_id)
        return None


def _publish_ceo_sessions_snapshot(*, agent, transcript_store, runtime_manager, state_store) -> None:
    registry = _registry(agent)
    if registry is None or transcript_store is None:
        return
    active_session_id = resolve_active_ceo_session_id(transcript_store, state_store)
    # 同步发布路径用带缓存的构建：TTL 命中免重建，未命中才就地构建一次。
    catalog = build_ceo_session_catalog_cached(
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
    resolved_is_running = _session_is_running(runtime_manager, key) if is_running is None else bool(is_running)
    if _is_channel_session_id(key):
        # 渠道会话必须走渠道形状构建器：本地构建器返回 None 后会回退到
        # `build_session_summary`，把渠道会话标成普通 web 会话推进全局补丁，
        # 前端会把它短暂插进本地会话列表。
        item = build_channel_ceo_session_item(
            transcript_store,
            key,
            active_session_id=active_session_id,
            is_running=resolved_is_running,
        )
        if item is None:
            return
    else:
        item = build_local_ceo_session_item(
            transcript_store,
            key,
            active_session_id=active_session_id,
            is_running=resolved_is_running,
        )
        if item is None:
            session = transcript_store.get_or_create(key)
            item = build_session_summary(
                session,
                is_active=key == active_session_id,
                is_running=resolved_is_running,
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


def external_upload_dir_for_session(session_id: str) -> Path:
    """The directory ``/api/v1`` writes channel attachments into.

    Containment for the read route has to mirror the writer exactly, including
    its ``workspace_path()`` root and its ``safe_filename`` session slug, or a
    legitimately stored clip would be refused.
    """
    return workspace_path() / EXTERNAL_UPLOAD_ROOT / safe_filename(str(session_id or ''))


def _guess_upload_mime_type(name: str, content_type: str | None = None) -> str:
    if isinstance(content_type, str) and content_type.strip():
        return content_type.strip()
    guessed, _ = mimetypes.guess_type(name)
    return guessed or 'application/octet-stream'


def _upload_kind(*, mime_type: str, name: str) -> str:
    mime = str(mime_type or '').lower()
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('audio/'):
        # 语音条要渲染成可播放的气泡，不是文件药丸；网页麦克风录出来的就是 WAV。
        return 'audio'
    guessed, _ = mimetypes.guess_type(name)
    if isinstance(guessed, str):
        if guessed.lower().startswith('image/'):
            return 'image'
        if guessed.lower().startswith('audio/'):
            return 'audio'
    return 'file'


def _serialize_upload_descriptor(path: Path, *, name: str, mime_type: str) -> dict[str, Any]:
    resolved = path.resolve()
    resolved_mime = _guess_upload_mime_type(name, mime_type)
    return {
        'name': name,
        'path': str(resolved),
        'relative_path': resolved.relative_to(data_root()).as_posix(),
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
    from g3ku.stt import audio as stt_audio

    original_name = safe_filename(str(upload.filename or '').strip()) or 'upload.bin'
    mime_type = _guess_upload_mime_type(original_name, getattr(upload, 'content_type', None))
    stored_name = original_name
    clip_bytes: bytes | None = None
    if _upload_kind(mime_type=mime_type, name=original_name) == 'audio':
        # 语音条是给人回放的素材，PCM 没有理由留在盘上：压成 MP3（实测 8×，0.2 秒）。
        # 只读这一格：超过录音上限的音频不是语音条（是有人拿附件口丢了个大文件），
        # 不转码、按原样流式落盘，免得为一个转码决定把整文件吸进内存。
        raw = upload.file.read(WEB_CEO_VOICE_UPLOAD_MAX_BYTES + 1)
        if len(raw) <= WEB_CEO_VOICE_UPLOAD_MAX_BYTES:
            encoded = await asyncio.to_thread(stt_audio.encode_to_mp3, raw)
            if encoded:
                clip_bytes = encoded
                stored_name = f"{Path(original_name).stem or 'voice'}.mp3"
                mime_type = 'audio/mpeg'
    target_dir = _session_upload_dir(session_id)
    target_path = target_dir / f"{uuid.uuid4().hex[:12]}_{stored_name}"
    if clip_bytes is None:
        upload.file.seek(0)
        with target_path.open('wb') as handle:
            shutil.copyfileobj(upload.file, handle)
    else:
        target_path.write_bytes(clip_bytes)
    item = _serialize_upload_descriptor(target_path, name=stored_name, mime_type=mime_type)
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
    output_dir = (workspace_path() / "output").resolve()
    try:
        candidate.relative_to(upload_dir)
    except ValueError:
        try:
            candidate.relative_to(output_dir)
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
        kind = str(item.get('kind') or '')
        if kind == 'audio':
            # 语音条的"内容"已经是正文里的转写文本；再给一行本地路径只会让模型
            # 以为还需要去打开一个文件。
            continue
        if kind == 'image':
            lines.append(f"- image: {item['name']} (local path: {item['path']})")
        else:
            lines.append(f"- file: {item['name']} (local path: {item['path']})")
    if len(lines) == 1:
        return ''
    lines.append('You may inspect the local file paths above when helpful.')
    return "\n".join(lines)


def _model_visible_uploads(uploads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything except the voice clip: the clip is playback material for the
    human, and its transcription already travels in the message text."""
    return [item for item in uploads or [] if str(item.get('kind') or '') != 'audio']


def _build_user_message(text: str, uploads: list[dict[str, Any]]) -> str | UserInputMessage:
    if not uploads:
        return text

    text_value = str(text or '')
    model_uploads = _model_visible_uploads(uploads)
    note = _uploaded_files_note(model_uploads)
    merged_text = f"{text_value}\n\n{note}" if (note and text_value) else (note or text_value)
    return UserInputMessage(
        content=merged_text or note or text_value,
        attachments=[str(item['path']) for item in model_uploads],
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
    inflight_turn = _rewrite_turn_snapshot_media(session_id, inflight_turn)
    preserved_turn = _rewrite_turn_snapshot_media(session_id, preserved_turn)
    payload: dict[str, Any] = {"inflight_turn": inflight_turn}
    if preserved_turn is not None:
        payload["preserved_turn"] = preserved_turn
    # 回合外的手动压缩没有 inflight turn 可挂进度，进行中状态只存在于会话级
    # `_compression_state`。这里随快照一并下发，前端才能在「刷新后重连」这一刻就恢复区分线：
    # 快照连接里 ceo.state 与 snapshot.ceo 的到达顺序不该决定界面画不画得出来。
    compression_snapshot = getattr(session, "_compression_snapshot", None)
    if callable(compression_snapshot):
        try:
            compression = compression_snapshot()
        except Exception:
            compression = None
        if isinstance(compression, dict) and compression:
            payload["compression"] = dict(compression)
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


@router.post('/ceo/transcribe')
async def transcribe_ceo_voice(file: UploadFile = File(...)):
    """Transcribe one composer voice recording and hand the text back.

    Bytes are never stored: unlike ``/ceo/uploads`` this request leaves nothing
    on disk, because a voice note is personal data the user has not chosen to
    attach to a session. Failures come back as ``200 + error_code`` rather than
    an HTTP error -- "no speech detected" is a valid outcome the composer has
    to render, not a transport fault.
    """
    from g3ku.stt import engine as stt_engine

    data = await file.read(WEB_CEO_VOICE_UPLOAD_MAX_BYTES + 1)
    if len(data) > WEB_CEO_VOICE_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                'code': 'voice_too_large',
                'size_bytes': len(data),
                'limit_bytes': WEB_CEO_VOICE_UPLOAD_MAX_BYTES,
                'message': '录音超过 2 MiB 上限，请缩短后再试。',
            },
        )
    result = await stt_engine.transcribe_bytes(
        data,
        filename=str(file.filename or 'voice'),
        mime_type=str(getattr(file, 'content_type', '') or ''),
        source='web-composer',
    )
    return result.as_dict()


@router.get('/ceo/external-upload-file')
async def get_ceo_external_upload_file(
    session_id: str = Query(...),
    path: str = Query(...),
):
    """Serve a file the External Agent API stored for this session.

    Channel voice clips live under ``external-uploads`` (the bridge pushes them
    through ``/api/v1`` like every other channel attachment), so a browser that
    has to *play* one needs this read lane — ``/ceo/uploads/file`` is rooted at
    the web upload directory and will not resolve it.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (workspace_path() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not str(session_id or "").strip():
        # 空 session 会让下面的 allowed 退化成 external-uploads 根目录，
        # 那等于任何会话的文件都能被任何请求读到。
        raise HTTPException(status_code=400, detail='invalid_session_id')
    allowed = external_upload_dir_for_session(session_id).resolve()
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='upload_path_outside_session_dir') from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail='upload_file_not_found')
    name = safe_filename(candidate.name) or candidate.name or 'attachment'
    return FileResponse(
        str(candidate),
        media_type=_guess_upload_mime_type(name),
        filename=name,
        content_disposition_type='inline',
    )


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


def _normalize_snapshot_attachments(message: dict[str, Any], session_id: str = "") -> list[dict[str, Any]]:
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
    # 渠道侧的语音条落在 external_attachments 里（桥经 /api/v1 推上来），
    # 历史上这个元数据从不到前端，所以只有可播放的音频被提升进气泡车道——
    # 把所有渠道文件都画成附件卡会顺手改掉现有渠道会话的显示形状。
    session_id = str(session_id or '').strip()
    for raw in list(metadata.get('external_attachments') or []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get('kind') or '').strip().lower() != 'audio':
            continue
        path = str(raw.get('path') or '').strip()
        if not path or not session_id:
            continue
        name = str(raw.get('name') or Path(path).name).strip() or Path(path).name or path
        params = urlencode({'session_id': session_id, 'path': path})
        items.append(
            {
                'path': path,
                'name': name,
                'mime_type': str(raw.get('mime_type') or '').strip() or 'audio/wav',
                'kind': 'audio',
                'url': f'/api/ceo/external-upload-file?{params}',
            }
        )
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


def _transcript_message_token_usage(value: Any) -> dict[str, int] | None:
    """校验 transcript 消息级持久化的轮次 token 用量（收尾时随 transcript 写入）。"""
    if not isinstance(value, dict):
        return None
    try:
        usage = {
            "input_tokens": int(value.get("input_tokens") or 0),
            "output_tokens": int(value.get("output_tokens") or 0),
            "cache_hit_tokens": int(value.get("cache_hit_tokens") or 0),
            "call_count": int(value.get("call_count") or 0),
        }
    except (TypeError, ValueError):
        return None
    if not any(usage[field] for field in ("input_tokens", "output_tokens", "cache_hit_tokens")):
        return None
    return usage


def _rewrite_turn_snapshot_media(
    session_id: str, snapshot: dict[str, Any] | None
) -> dict[str, Any] | None:
    if isinstance(snapshot, dict):
        snapshot = dict(snapshot)
        text = snapshot.get('assistant_text')
        if isinstance(text, str):
            snapshot['assistant_text'] = rewrite_assistant_media_content(session_id, text)
    return snapshot


def _session_fully_stable_for_history_edit(session: Any, turn_payload: dict[str, Any] | None) -> bool:
    """编辑重发/Fork 按钮的防御型显示总开关。

    任何进行中请求/回合的迹象（用户轮、heartbeat/cron 内部轮、排队 follow-up、
    待审批中断、blocking tool）都要求整体隐藏按钮；端点侧另有 409 复验。
    """
    state = getattr(session, "state", None)
    if bool(getattr(state, "is_running", False)):
        return False
    if str(getattr(state, "status", "") or "").strip().lower() == "running":
        return False
    if bool(getattr(state, "paused", False)):
        return False
    if list(getattr(state, "pending_interrupts", []) or []):
        return False
    if list(getattr(state, "queued_follow_up_messages", []) or []):
        return False
    blocking = getattr(session, "has_blocking_tool_execution", None)
    if callable(blocking):
        try:
            if bool(blocking()):
                return False
        except Exception:
            return False
    payload = turn_payload if isinstance(turn_payload, dict) else {}
    for lane in ("inflight_turn", "preserved_turn"):
        lane_payload = payload.get(lane)
        if isinstance(lane_payload, dict) and lane_payload:
            return False
    return True


def _session_edit_fork_gates(
    session: Any,
    session_id: str,
    messages: Any,
    *,
    turn_payload: dict[str, Any] | None,
    is_channel_session: bool,
    agent: Any,
) -> tuple[dict[int, bool] | None, dict[int, bool] | None]:
    """计算快照消息级门槛，返回 ``(编辑重发门槛, Fork 门槛)``（键 = 原始转录下标）。

    两者共用同一套内容判据（任务派发 / run 首条 / 边界快照可用），差别只在运行态：
    编辑重发要改源会话转录，必须等会话没有活的执行体；Fork 是只读前缀复制、源会话
    零变更（``fork_ceo_session`` 端点本来就不查运行态），所以回合在跑、等审批、压缩在途
    都照样给资格。
    某一项为 None 表示不下发该类 flag：渠道会话两项都 None，非稳定态只 None 掉编辑项。
    """
    key = str(session_id or "").strip()
    if is_channel_session or not key.startswith("web:"):
        return None, None
    from g3ku.runtime.web_ceo_history_edit import compute_edit_fork_gates, legacy_task_created_ats
    from g3ku.runtime.web_ceo_sessions import list_turn_boundary_snapshot_turn_ids

    raw_messages = list(messages or [])
    fork_gates = compute_edit_fork_gates(
        raw_messages,
        enabled=True,
        task_created_ats=legacy_task_created_ats(agent, key, raw_messages),
        available_boundary_turn_ids=list_turn_boundary_snapshot_turn_ids(key),
    )
    if not _session_fully_stable_for_history_edit(session, turn_payload):
        return None, fork_gates
    return fork_gates, fork_gates


def _edit_fork_eligible_turn_ids(
    raw_messages: list[Any],
    gates: dict[int, bool] | None,
) -> list[str]:
    """把按转录下标编码的门槛翻成前端可匹配的 turn_id 列表。

    门槛只落在 run 首条用户消息上，因此一个合格下标对应一个 turn_id；
    旧转录里没有 turn_id 的行无法作为按钮键使用（前端按 turn_id 定位），直接跳过。
    """
    turn_ids: list[str] = []
    for index in sorted(gates or {}):
        if not gates.get(index):
            continue
        raw = raw_messages[index] if 0 <= index < len(raw_messages) else None
        turn_id = _snapshot_message_turn_id(raw if isinstance(raw, dict) else None)
        if turn_id and turn_id not in turn_ids:
            turn_ids.append(turn_id)
    return turn_ids


def _snapshot_compression_marker(metadata: Any) -> dict[str, Any]:
    """把转录里的上下文压缩区分线行翻成 UI 载荷。

    键名/取值与 session_agent.CONTEXT_COMPRESSION_MARKER_* 一致；这里用字面量是
    为避免与 session_agent 形成循环导入（同 web_ceo_sessions 里 discarded 的做法）。
    """
    if str(metadata.get("kind") or "").strip().lower() != "context_compression":
        return {}
    state = str(metadata.get("compression_state") or "").strip().lower()
    if state not in {"completed", "paused"}:
        return {}
    marker: dict[str, Any] = {
        "state": state,
        "source": str(metadata.get("source") or "").strip().lower(),
    }
    stats = metadata.get("stats")
    if isinstance(stats, dict) and stats:
        marker["stats"] = dict(stats)
    return marker


def _snapshot_row_transcript_view(
    raw: dict[str, Any],
    role: str,
    storage_cursor: dict[str, Any],
) -> dict[str, Any]:
    """三态解析一条转录行的投影视图：stage_window 直通、cc_upsert 重放一步、旧格式投影一次。

    存储重放链必须遍历所有带轨道的行（含 ui_visible False 的 heartbeat/cron 运行轮——
    写入侧的 upsert 是相对物理上一轨道行编码的），与只遍历可见行的出帧 delta 链分开。"""
    if role != 'assistant':
        return {}
    stored = raw.get('canonical_context')
    if isinstance(stored, dict) and stored:
        if str(raw.get('canonical_context_projection') or '').strip() == TRANSCRIPT_PROJECTION_MODE:
            return stored
        return _project_canonical_context_for_transcript(stored)
    upsert = raw.get('cc_upsert')
    if isinstance(upsert, dict):
        return _apply_cc_upsert(storage_cursor, upsert)
    return {}


def _build_ceo_snapshot(
    messages: list[dict[str, Any]] | None,
    *,
    inflight_turn: dict[str, Any] | None = None,
    session_id: str | None = None,
    edit_fork_gates: dict[int, bool] | None = None,
    fork_gates: dict[int, bool] | None = None,
) -> list[dict[str, Any]]:
    inflight_payload = inflight_turn if isinstance(inflight_turn, dict) else {}
    inflight_status = str(inflight_payload.get("status") or "").strip().lower()
    hide_pending_users = inflight_status in {"running", "in_progress", "active"}
    items: list[dict[str, Any]] = []
    # 滚动保存上一条 assistant 行的转录投影视图：快照按序回放，逐行重新投影会
    # 让整帧构建退化为平方级（渠道会话单转录数十 MB 时实测 12s+）。
    previous_transcript_view: dict[str, Any] = {}
    # 存储重放游标：cc_upsert 行相对物理上一轨道行编码，须连同隐藏行一起推进。
    storage_view_cursor: dict[str, Any] = {}
    # 按 turn_id 聚合的请求工件扫描要 glob 整个 artifact 目录并逐文件解析
    # （大会话实测 300+ 文件 / 70MB ≈ 1s+）。transcript 级 usage 自收尾起随每轮
    # 持久化：只有当存在"带 turn_id 却无 usage"的 assistant 行（旧转录）时才付
    # 这笔扫描。
    needs_artifact_usage = any(
        isinstance(raw, dict)
        and str(raw.get('role') or '').strip().lower() == 'assistant'
        and not isinstance(raw.get('usage'), dict)
        and (
            str(raw.get('turn_id') or '').strip()
            or str(
                (raw.get('metadata') or {}).get('_transcript_turn_id')
                if isinstance(raw.get('metadata'), dict)
                else ''
            ).strip()
        )
        for raw in list(messages or [])
    )
    usage_by_turn = (
        read_session_turn_token_usage(session_id)
        if session_id and needs_artifact_usage
        else {}
    )
    for index, raw in enumerate(list(messages or [])):
        if not isinstance(raw, dict):
            continue
        metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        role = str(raw.get('role') or '').strip().lower()
        if role not in {'user', 'assistant', 'system'}:
            continue
        current_view = _snapshot_row_transcript_view(raw, role, storage_view_cursor)
        if current_view:
            storage_view_cursor = current_view
        if metadata.get('ui_visible') is False:
            continue
        if is_internal_ceo_user_message(raw):
            continue
        if hide_pending_users and role == "user" and _snapshot_message_transcript_state(raw) == "pending":
            continue
        content = _history_text(raw.get('content'))
        if role == 'user':
            raw_text = metadata.get('web_ceo_raw_text')
            if isinstance(raw_text, str) and isinstance(metadata.get('web_ceo_uploads'), list):
                content = raw_text
        if role == 'assistant' and session_id:
            content = rewrite_assistant_media_content(session_id, content)
        attachments = _normalize_snapshot_attachments(raw, session_id) if role == 'user' else []
        canonical_context = (
            raw.get('canonical_context')
            if role == 'assistant' and isinstance(raw.get('canonical_context'), dict)
            else None
        )
        compression = (
            dict(raw.get('compression'))
            if role == 'assistant' and isinstance(raw.get('compression'), dict)
            else {}
        )
        if not content and not attachments and not current_view and not compression:
            continue
        item = {'role': role, 'content': content}
        if role == 'assistant':
            status = str(raw.get('status') or '').strip().lower()
            if status:
                item['status'] = status
            # 静默回合的空 assistant 行只为承载阶段轨道；没有轨道就没有可展示的内容，
            # 整行跳过，避免前端渲染出一个空气泡。旧转录里的占位文案同样按静默处理。
            if metadata.get('silent_reply') is True or content == _LEGACY_SILENT_REPLY_TEXT:
                # 保留正文：它是折叠行「展开」要显示的内容，也是模型下一轮的痕迹。
                # 空正文且无轨道的行由上方的空行过滤统一跳过。
                item['silent_reply'] = True
                silent_reason = str(metadata.get('silent_reason') or '').strip()
                if silent_reason:
                    item['silent_reason'] = silent_reason
                if content == _LEGACY_SILENT_REPLY_TEXT:
                    item['content'] = ''
        turn_id = str(raw.get('turn_id') or raw.get('metadata', {}).get('_transcript_turn_id') or '').strip() if isinstance(raw.get('metadata'), dict) else str(raw.get('turn_id') or '').strip()
        if turn_id:
            item['turn_id'] = turn_id
        if role == 'user':
            if edit_fork_gates and edit_fork_gates.get(index):
                item['can_edit_fork'] = True
            if fork_gates and fork_gates.get(index):
                item['can_fork'] = True
        if role == 'assistant' and any(
            str(task_id or '').startswith('task:')
            for task_id in list(metadata.get('task_ids') or [])
        ):
            item['task_dispatched'] = True
        timestamp = raw.get('timestamp')
        if isinstance(timestamp, str) and timestamp.strip():
            item['timestamp'] = timestamp.strip()
        if attachments:
            item['attachments'] = attachments
        if current_view:
            # 出帧只携带 delta：前端轨道渲染是 delta 优先，逐行全量累积 cc 是首帧
            # payload 平方级膨胀的主项（QQ 渠道会话实测 69 MB 中 51 MB 前端直接
            # 丢弃）。空 delta 以 {} 落键——前端据此渲染纯文本气泡而不回退重画旧
            # 轨道（org_graph_app.js hasDelta 语义）。body 回填只对带全量 cc 的行
            # 用存量原文（旧 raw 行保留未截断正文），cc_upsert 行的正文就在视图里。
            item['canonical_context_delta'] = _ui_canonical_context_delta_from_views(
                previous_transcript_view,
                current_view,
                canonical_context if canonical_context is not None else current_view,
            )
            previous_transcript_view = current_view
        if compression:
            item['compression'] = compression
        if role == 'assistant':
            # transcript 级 usage 优先（收尾时持久化，不随请求工件修剪丢失），
            # 旧 transcript 无该字段时回退按 turn_id 聚合的工件用量。
            transcript_usage = _transcript_message_token_usage(raw.get('usage'))
            turn_usage = transcript_usage or (usage_by_turn.get(turn_id) if turn_id else None)
            if turn_usage:
                item['usage'] = turn_usage
        marker = _snapshot_compression_marker(metadata)
        if marker:
            item['compression_marker'] = marker
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


def _assistant_canonical_context(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    if str(message.get("role") or "").strip().lower() != "assistant":
        return {}
    return _canonical_context_copy(message.get("canonical_context"))


def _latest_persisted_assistant_canonical_context(persisted_session: Any | None) -> dict[str, Any]:
    messages = list(getattr(persisted_session, "messages", None) or [])
    for index in range(len(messages) - 1, -1, -1):
        raw = messages[index]
        canonical_context = _assistant_canonical_context(raw)
        if canonical_context:
            return canonical_context
        if (
            isinstance(raw, dict)
            and str(raw.get("role") or "").strip().lower() == "assistant"
            and isinstance(raw.get("cc_upsert"), dict)
        ):
            # delta 存储行：live/final 基线要的是该行自己的累积视图。
            view = _materialize_transcript_view(messages, index)
            if view:
                return dict(view)
    return {}


def _with_canonical_context_delta(payload: dict[str, Any] | None, previous_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    next_payload = copy.deepcopy(payload)
    canonical_context = next_payload.pop('canonical_context', None)
    if not isinstance(canonical_context, dict) or not canonical_context:
        next_payload.pop('canonical_context_delta', None)
        return next_payload
    delta = _ui_canonical_context_delta(previous_context, canonical_context)
    if delta:
        next_payload['canonical_context_delta'] = delta
    else:
        next_payload.pop('canonical_context_delta', None)
    # 全量投影只属于 final 帧：live 轨道读的是 delta，附上整份工作集既没人用，
    # 又让每个工具事件都为 500+ 个阶段付一次投影（QQ 会话实测单帧 462KB / 160ms）。
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
    return _ui_canonical_context_delta(
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
    if bool(data.get("silent_reply")):
        return False
    return bool(str(data.get("text") or "").strip())


def _is_internal_ack_message_end(payload: dict[str, Any] | None) -> bool:
    """内部回合「本轮无话可说」的判定：改读 silent 工具信号，不再匹配文本。

    旧判据是 `text == "HEARTBEAT_OK"`，即模型用文案哨兵收尾时给前端推一条 ack 而不是
    空气泡。现在同一个意图由 `silent` 工具表达，所以这里换成 flag；排除 task_terminal
    心跳那一支（它走回复通道，见 heartbeat/session_service 的 ack 投递）。
    """
    data = payload if isinstance(payload, dict) else {}
    if str(data.get("role") or "").strip().lower() != "assistant":
        return False
    if not bool(data.get("silent_reply")):
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
    # 目录构建要遍历全部会话转录（冷缓存时可达数十秒），必须卸载出事件
    # 循环，否则一次 /ws/ceo 重连就会把任务大厅的列表/心跳请求全部挂起。
    initial_catalog = await build_ceo_session_catalog_async(
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
    # 在连接期解析一次：注册表条目决定了本 socket 能否输入，socket 生命周期内不必再查。
    external_entry = _external_entry_for_channel_input(session_id) if is_channel_session else None

    def _load_persisted_session():
        # get_or_create 冷缓存时逐行解析整份转录（渠道会话可达数十 MB），
        # 与目录构建一样必须在工作线程执行。
        if is_channel_session:
            return transcript_store.get_or_create(session_id) if session_path.exists() else None
        persisted = (
            create_web_ceo_session(transcript_store, session_id=session_id)
            if not session_path.exists()
            else transcript_store.get_or_create(session_id)
        )
        if ensure_ceo_session_metadata(persisted):
            transcript_store.save(persisted)
        return persisted

    persisted_session = await run_off_event_loop(_load_persisted_session)
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
    # 快照构建遍历整份转录消息做投影/重写，同样卸载出事件循环。
    # 编辑/Fork 门槛在同一次卸载内计算（转录行走 + 边界快照目录列举 + 可选任务表兜底）。
    def _compose_ceo_snapshot() -> list[dict[str, Any]]:
        raw_messages = getattr(persisted_session, 'messages', [])
        edit_gates, fork_gates = _session_edit_fork_gates(
            session,
            session_id,
            raw_messages,
            turn_payload=turn_payload,
            is_channel_session=is_channel_session,
            agent=agent,
        )
        return _build_ceo_snapshot(
            raw_messages,
            inflight_turn=turn_payload.get("inflight_turn") if isinstance(turn_payload, dict) else None,
            session_id=session_id,
            edit_fork_gates=edit_gates,
            fork_gates=fork_gates,
        )

    persisted_messages = await run_off_event_loop(_compose_ceo_snapshot)
    current_turn_task: asyncio.Task[Any] | None = None
    closed = asyncio.Event()
    _send_lock = asyncio.Lock()
    turn_patch_gate: dict[str, Any] = {"last_sent_at": 0.0, "pending": None}

    async def _safe_send(payload: dict[str, Any]) -> None:
        # websockets legacy 协议只允许一个写者：三条 sender 任务加握手期的直发并发
        # send() 会在 _drain_helper 里踩 `assert waiter is None or waiter.cancelled()`，
        # 帧被吞掉而 socket 还开着（实盘 09-24 一天 418 次），界面就只能等重连补快照。
        async with _send_lock:
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

    async def _send_turn_patch_now() -> None:
        turn_patch_gate["last_sent_at"] = asyncio.get_running_loop().time()
        try:
            persisted_session = transcript_store.get_or_create(session_id)
        except Exception:
            persisted_session = None
        await _push_stream_event('ceo.turn.patch', _build_live_turn_payload(session, session_id, persisted_session))

    async def _flush_pending_turn_patch() -> None:
        # 回合结束前先把挂起的补丁补出去：补丁必须排在 final 之前，既不能让快回合
        # 整帧丢掉轨道，也不能在 final 之后再来一帧 running|paused 把前端顶成新回合。
        pending = turn_patch_gate.get("pending")
        turn_patch_gate["pending"] = None
        if pending is None or pending.done():
            return
        pending.cancel()
        await _send_turn_patch_now()

    def _drop_pending_turn_patch() -> None:
        pending = turn_patch_gate.get("pending")
        if pending is not None and not pending.done():
            pending.cancel()
        turn_patch_gate["pending"] = None

    async def _flush_turn_patch_after(delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            turn_patch_gate["pending"] = None
        if closed.is_set():
            return
        try:
            await _send_turn_patch_now()
        except Exception:
            logger.exception('CEO websocket deferred turn patch failed for {}', session_id)

    async def _push_turn_patch() -> None:
        now = asyncio.get_running_loop().time()
        elapsed = now - float(turn_patch_gate.get("last_sent_at") or 0.0)
        if elapsed >= _CEO_TURN_PATCH_MIN_INTERVAL_S:
            await _send_turn_patch_now()
            return
        pending = turn_patch_gate.get("pending")
        if pending is not None and not pending.done():
            return
        turn_patch_gate["pending"] = asyncio.create_task(
            _flush_turn_patch_after(_CEO_TURN_PATCH_MIN_INTERVAL_S - elapsed)
        )

    async def _push_edit_fork_gates() -> None:
        # 编辑/Fork 门槛原本只随连接时的 snapshot.ceo 下发一次：回合收尾后转录里
        # 才既有本轮 assistant 行（任务派发要收回资格）又已清掉 inflight sidecar（稳定态
        # 判定要放行），但没有任何通道再算一遍，按钮只能等用户手动刷新。这里每个
        # state_snapshot 都按同一套门槛补发一次，前端据此给缓存行打/收 flag。
        if is_channel_session:
            return
        try:
            persisted_session = transcript_store.get_or_create(session_id)
        except Exception:
            persisted_session = None
        turn_payload = _build_live_turn_payload(session, session_id, persisted_session)
        raw_messages = list(getattr(persisted_session, 'messages', []) or [])
        edit_gates, fork_gates = await run_off_event_loop(
            lambda: _session_edit_fork_gates(
                session,
                session_id,
                raw_messages,
                turn_payload=turn_payload,
                is_channel_session=is_channel_session,
                agent=agent,
            )
        )
        await _push_stream_event(
            'ceo.edit_fork.gates',
            {
                'turn_ids': _edit_fork_eligible_turn_ids(raw_messages, edit_gates),
                'fork_turn_ids': _edit_fork_eligible_turn_ids(raw_messages, fork_gates),
            },
        )

    def _current_session_is_running() -> bool:
        status = str(getattr(session.state, 'status', '') or '').strip().lower()
        return bool(getattr(session.state, 'is_running', False)) or status == 'running'

    def _inbound_hold() -> str:
        """running 的超集：还包含跑在回合外的手动压缩。压缩在途时 is_running 是假的
        false，此时新消息会起一个真回合并用压缩前的种子覆盖刚落地的摘要基线。
        判定本体在 RuntimeAgentSession.frontdoor_inbound_hold，与渠道车道同一个问题。"""
        hold = getattr(session, 'frontdoor_inbound_hold', None)
        return str(hold() or '').strip() if callable(hold) else ''

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
            error_message = str(exc).strip()
            if not error_message and isinstance(snapshot, dict):
                last_error = snapshot.get("last_error")
                if isinstance(last_error, dict):
                    error_message = str(last_error.get("message") or "").strip()
            if not error_message and isinstance(exc, MemoryError):
                error_message = "运行时内存不足，未能完成当前轮次"
            await _flush_pending_turn_patch()
            await _push_stream_event(
                'ceo.error',
                {
                    'code': 'turn_failed',
                    'message': error_message or 'unknown error',
                    'source': str((snapshot or {}).get('source') or 'user').strip().lower() or 'user',
                    'turn_id': str((snapshot or {}).get('turn_id') or '').strip(),
                },
            )
            return

    async def _handle_channel_user_input(user_messages: list[UserInputMessage]) -> None:
        """渠道会话的网页输入：提交转投外部车道，本 socket 只负责回执与侧栏状态。

        为什么不能就地用 `_invoke_user_turn`：对外事件 relay 只挂在
        `external_turns._execute_turn` 上，WS 原生 prompt 路径不挂。走原生路会造出
        「网页 feed 有回复、会话 hub 上没有 reply.final、渠道 pump 无事可投」的形状
        （合同见 docs/architecture/external-agent-api.md「回合契约」）。

        排队与起回合的裁决权一并交给 `submit`（它的 hold 判定覆盖「有回合在跑或手动压缩
        在途」），本分支不重复排队；也不登记 current_turn_task —— 回合 task 归外部车道
        所有（注册在 None 键），暂停走既有的 `session.pause` 通道。
        渠道转录可达数十 MB，这里一次都不碰转录存储：message_count 不填。
        """
        if _pending_tool_approval_interrupts(session, session_id, None):
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
            return
        if bool(getattr(session, 'has_blocking_tool_execution', lambda: False)()):
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
            return
        try:
            service = get_external_turn_service()
        except Exception:
            await _safe_send(
                build_envelope(
                    channel='ceo',
                    session_id=session_id,
                    type='error',
                    data={'code': 'task_service_unavailable'},
                )
            )
            return

        any_queued = False
        for item in list(user_messages or []):
            try:
                result = await service.submit(entry=external_entry, user_message=item)
            except Exception as exc:
                await _safe_send(
                    build_envelope(
                        channel='ceo',
                        session_id=session_id,
                        type='error',
                        data={'code': 'task_service_unavailable', 'message': str(exc)},
                    )
                )
                return
            any_queued = any_queued or str(result.get('status') or '') == 'queued'

        _publish_ceo_session_patch(
            agent=agent,
            transcript_store=transcript_store,
            runtime_manager=runtime_manager,
            state_store=state_store,
            session_id=session_id,
            preview_text=_history_text(user_messages[-1].content),
            is_running=True,
        )
        if any_queued:
            # queue_follow_up_batch 不发会话事件，网页自己排队时是后续回合事件顺带把
            # ceo.state 带来的；本车道排队意味着本地没有回合在跑，不补这一帧，操作者
            # 投出去的那条要等渠道侧下一次事件才出现在候选发送条里。
            await _push_stream_event('ceo.state', {'state': session.state_dict()})

    async def sender(source_queue: asyncio.Queue[dict[str, Any]]) -> None:
        # 单帧写失败（载荷不可序列化、连接半开）绝不能让这个任务死掉：三条 sender 都是
        # 静默退出，socket 还开着、客户端收不到 close，也就不会自动重连，界面会永久停在
        # 半截的回合上。所以逐帧兜住并计数，连续失败才认定链路已废——主动关掉 socket，
        # 让前端的 onclose 走重连重取快照。
        failures = 0
        while True:
            payload = await source_queue.get()
            try:
                await _safe_send(payload)
            except WebSocketChannelClosed:
                raise
            except Exception:
                failures += 1
                logger.exception(
                    'CEO websocket failed to send frame {} to {} ({}/{} consecutive)',
                    str((payload or {}).get('type') or ''),
                    session_id,
                    failures,
                    _SENDER_CONSECUTIVE_FAILURE_LIMIT,
                )
                if failures >= _SENDER_CONSECUTIVE_FAILURE_LIMIT:
                    closed.set()
                    await websocket_close(websocket, code=1011)
                    return
                continue
            failures = 0

    async def relay_session_event(event: AgentEvent) -> None:
        # RuntimeAgentSession._emit 逐个 await 订阅者且不做捕获：转发层里漏一个异常，
        # 就会顺着 message_end/state_snapshot 把正在收尾的回合打断，前端只留下一个永不
        # 结束的回合。所以本层任何失败都只记日志，绝不再往上抛。
        try:
            await _relay_session_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('CEO websocket relay failed for {} event {}', session_id, event.type)

    async def _relay_session_event(event: AgentEvent) -> None:
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
            # 门槛帧不跟着 paused 一起跳过：Fork 资格不看运行态，而暂停/等审批恰恰是
            # 输入被闸门挡住、只剩 Fork 可用的时刻。
            await _push_edit_fork_gates()
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
            # 静默回合（模型调用 silent 工具）走同一条 final 通道，只是回复文本置空：
            # 早退会让 final 丢掉 canonical_context / user_messages / usage，前端就没有
            # 阶段轨道可收尾，本回合的阶段与工具调用会被整段吞掉。外部渠道仍由 external
            # relay 依据 silent_reply 跳过，不会投递到 QQ。
            silent_reply = bool(payload.get('silent_reply'))
            if not silent_reply and not _should_forward_message_end(payload):
                return
            # 静默回合不再抹正文：那正文就是给模型的痕迹，也是前端折叠行「展开」要显示的
            # 内容。不外发由 silent_reply flag 决定（外部渠道与 cron 各自据它闸门），
            # 不再靠把文本清空。
            text = str(payload.get('text') or '').strip()
            silent_reason = str(payload.get('silent_reason') or '').strip()
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
            turn_usage = None
            turn_usage_map = getattr(session, "_frontdoor_turn_usage", None)
            if isinstance(turn_usage_map, dict) and turn_id:
                turn_usage = turn_usage_map.get(turn_id) or None
            if not turn_usage and isinstance(snapshot, dict):
                turn_usage = snapshot.get("usage") or None
            if _is_internal_ack_message_end(payload):
                reason = str(payload.get("heartbeat_reason") or "heartbeat_ok").strip() or "heartbeat_ok"
                await _flush_pending_turn_patch()
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
            await _flush_pending_turn_patch()
            await _push_stream_event(
                'ceo.reply.final',
                {
                    'text': rewrite_assistant_media_content(session_id, text),
                    'source': source,
                    'turn_id': turn_id,
                    **({'silent_reply': True} if silent_reply else {}),
                    **({'silent_reason': silent_reason} if silent_reply and silent_reason else {}),
                    **({'user_messages': user_messages} if user_messages else {}),
                    **({'usage': turn_usage} if turn_usage else {}),
                    **final_reply_canonical_merge(canonical_context, canonical_context_delta),
                },
            )
            _publish_ceo_session_patch(
                agent=agent,
                transcript_store=transcript_store,
                runtime_manager=runtime_manager,
                state_store=state_store,
                session_id=session_id,
                preview_text=None if silent_reply else text,
                is_running=False,
            )
            return
        if event.type == 'frontdoor_stage_synced':
            # 图节点边界同步后主动推送一次 live turn patch:阶段/轮次状态刚刷新,
            # 若等到下一工具事件才推,新开阶段的 delta 会为空导致前端清空时间线
            await _push_turn_patch()
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
        # Session catalog goes out BEFORE the transcript snapshot: the snapshot
        # can be tens of megabytes on long channel sessions (canonical-context
        # payloads), and the list must never be queued behind it — otherwise the
        # sidebar stays stale until the whole transcript drains (or the socket
        # drops mid-transfer). Frontend envelope handling is order-independent.
        initial_catalog = await build_ceo_session_catalog_async(
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
        await _safe_send(build_envelope(channel='ceo', session_id=session_id, type='ceo.state', data={'state': session.state_dict()}))
        await _safe_send(
            build_envelope(
                channel='ceo',
                session_id=session_id,
                type='snapshot.ceo',
                data={'messages': persisted_messages, **turn_payload},
            )
        )
        # 重连也是"会话回到空闲"的时刻之一（进程重启后第一次连上来就走这里，构造期已把
        # 转录里仍是 pending 的条目接回队列）。起任务而不是 await：这条回合会把握手后的
        # 第一个 read 挡到回合结束。
        dispatch_queued = getattr(session, 'dispatch_queued_follow_ups_if_idle', None)
        if callable(dispatch_queued):
            asyncio.create_task(dispatch_queued(source='ws_reconnect'))
        while True:
            if closed.is_set():
                break
            data = await websocket_receive_json(websocket)
            message_type = str(data.get('type') or '')
            if message_type == 'client.resume_interrupt':
                if _current_session_is_running() or _inbound_hold() or (current_turn_task is not None and not current_turn_task.done()):
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
            if is_channel_session and external_entry is None:
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
            if is_channel_session:
                await _handle_channel_user_input(user_messages)
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
            if _current_session_is_running() or _inbound_hold() or (current_turn_task is not None and not current_turn_task.done()):
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
        _drop_pending_turn_patch()
        sender_task.cancel()
        global_sender_task.cancel()
        stream_task.cancel()
        await asyncio.gather(sender_task, global_sender_task, stream_task, return_exceptions=True)
        await _maybe_await(service.registry.unsubscribe_ceo(session_id, queue))
        await _maybe_await(service.registry.unsubscribe_global_ceo(global_queue))
