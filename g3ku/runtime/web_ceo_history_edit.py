"""Web CEO 会话历史截断与 Fork（用户消息编辑重发 / Fork 会话）。

本模块是「编辑重发 / Fork」的服务端核心：

- 截断数据源 = 每轮边界快照（``.g3ku/web-ceo-turn-boundaries/<session>/<turn_id>.json.gz``，
  由 ``RuntimeAgentSession._sync_completed_continuity_snapshot`` 每轮 upsert，
  只保留最近 ``TURN_BOUNDARY_SNAPSHOT_KEEP`` 轮）。不做任何启发式基线重建：
  prev_turn 边界快照缺失即视为不合格（前端不显示按钮、端点 409）。
- 截断点必须落在干净的轮边界上：被点击消息必须是其所在 user-run
  （极大连续可见 user 消息段）的首条；批次兄弟与中途消费的 follow-up
  没有自己的轮边界，不显示按钮。
- 异步任务门槛（严格判定）：被点击消息之前、或被点击消息自己触发的
  回复轮中实际成功创建过异步任务（转录 assistant ``metadata.task_ids``），
  则该消息不可编辑/Fork。legacy 转录（整份无 task_ids 字段）用
  ``list_tasks_for_session`` 的 created_at 时间戳兜底。

所有函数只做同步文件/内存操作，由 API 层负责在 ``_turn_lock`` 内调用。
"""

from __future__ import annotations

import copy
import json
import mimetypes
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.runtime.web_ceo_sessions import (
    DEFAULT_CEO_SESSION_TITLE,
    actual_request_dir_for_session,
    clear_actual_request_history,
    clear_inflight_turn_snapshot,
    clear_paused_execution_context,
    clear_turn_boundary_snapshots,
    ensure_ceo_session_metadata,
    is_internal_ceo_user_message,
    list_turn_boundary_snapshot_turn_ids,
    new_web_ceo_session_id,
    read_turn_boundary_snapshot,
    summarize_preview_text,
    upload_dir_for_session,
    workspace_path,
    write_completed_continuity_snapshot,
    write_turn_boundary_snapshot,
)
from g3ku.utils.helpers import safe_filename

USER_EDIT_TRUNCATION_REASON = "user_edit_truncation"
_INTERNAL_ASSISTANT_SOURCES = {"heartbeat", "cron"}


class HistoryEditError(Exception):
    """编辑重发 / Fork 校验或执行失败（携带 HTTP 语义的错误码）。"""

    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = str(code or "history_edit_failed")
        self.status_code = int(status_code or 409)


def message_role(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("role") or "").strip().lower()


def message_metadata(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def message_turn_id(message: Any) -> str:
    """转录消息的轮标识：顶层 turn_id 优先，其次 metadata._transcript_turn_id。"""
    if not isinstance(message, dict):
        return ""
    direct = str(message.get("turn_id") or "").strip()
    if direct:
        return direct
    return str(message_metadata(message).get("_transcript_turn_id") or "").strip()


def message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content or "").strip()


def is_visible_user_message(message: Any) -> bool:
    if message_role(message) != "user":
        return False
    if is_internal_ceo_user_message(message):
        return False
    return message_metadata(message).get("ui_visible") is not False


def is_internal_assistant_message(message: Any) -> bool:
    if message_role(message) != "assistant":
        return False
    return str(message_metadata(message).get("source") or "").strip().lower() in _INTERNAL_ASSISTANT_SOURCES


def transcript_task_ids(message: Any) -> list[str]:
    return [
        str(item).strip()
        for item in list(message_metadata(message).get("task_ids") or [])
        if str(item or "").strip().startswith("task:")
    ]


def transcript_has_task_ids_field(messages: list[Any]) -> bool:
    """整份转录是否出现过 task_ids 字段（legacy 兜底的启用开关）。"""
    for raw in list(messages or []):
        if not isinstance(raw, dict):
            continue
        if message_role(raw) != "assistant":
            continue
        if "task_ids" in message_metadata(raw):
            return True
    return False


@dataclass
class BoundaryResolution:
    boundary_index: int
    eligible: bool
    reason: str
    run_indices: list[int] = field(default_factory=list)
    removed_turn_ids: list[str] = field(default_factory=list)
    boundary_message: dict[str, Any] = field(default_factory=dict)
    prev_turn_id: str = ""


def _expand_visible_user_run(messages: list[Any], index: int) -> tuple[int, int]:
    start = index
    while start - 1 >= 0 and is_visible_user_message(messages[start - 1]):
        start -= 1
    end = index
    while end + 1 < len(messages) and is_visible_user_message(messages[end + 1]):
        end += 1
    return start, end


def resolve_truncation_boundary(
    messages: list[Any],
    turn_id: str,
    *,
    available_boundary_turn_ids: set[str] | None = None,
) -> BoundaryResolution | None:
    """定位被点击的用户消息并解析其所在 user-run 的截断边界。

    返回 None 表示转录中不存在该 turn_id 对应的可见用户消息（端点应 404）。
    ``eligible=False`` 时 ``reason`` 给出拒绝码：
    - ``turn_not_run_first``：被点击消息不是所在 user-run 的首条；
    - ``boundary_unavailable``：prev_turn 的边界快照缺失（超出保留窗口或旧数据）。
    """
    normalized_turn_id = str(turn_id or "").strip()
    msgs = list(messages or [])
    if not normalized_turn_id or not msgs:
        return None
    clicked_index: int | None = None
    for index, raw in enumerate(msgs):
        if not isinstance(raw, dict):
            continue
        if not is_visible_user_message(raw):
            continue
        if message_turn_id(raw) != normalized_turn_id:
            continue
        clicked_index = index
        break
    if clicked_index is None:
        return None
    run_start, run_end = _expand_visible_user_run(msgs, clicked_index)
    run_indices = list(range(run_start, run_end + 1))
    removed_turn_ids: list[str] = []
    for raw in msgs[run_start:]:
        for candidate in (
            message_turn_id(raw),
            str(message_metadata(raw).get("_transcript_turn_id") or "").strip(),
        ):
            if candidate and candidate not in removed_turn_ids:
                removed_turn_ids.append(candidate)
    prev_turn_id = message_turn_id(msgs[run_start - 1]) if run_start > 0 else ""
    eligible = True
    reason = ""
    if clicked_index != run_start:
        eligible = False
        reason = "turn_not_run_first"
    elif run_start > 0:
        if not prev_turn_id:
            eligible = False
            reason = "boundary_unavailable"
        elif available_boundary_turn_ids is not None and prev_turn_id not in available_boundary_turn_ids:
            eligible = False
            reason = "boundary_unavailable"
    return BoundaryResolution(
        boundary_index=run_start,
        eligible=eligible,
        reason=reason,
        run_indices=run_indices,
        removed_turn_ids=removed_turn_ids,
        boundary_message=dict(msgs[run_start]) if isinstance(msgs[run_start], dict) else {},
        prev_turn_id=prev_turn_id,
    )


def visible_user_run_first_indices(messages: list[Any]) -> set[int]:
    """所有"所在 user-run 首条"的可见用户消息下标集合。"""
    firsts: set[int] = set()
    msgs = list(messages or [])
    index = 0
    while index < len(msgs):
        if is_visible_user_message(msgs[index]):
            start, end = _expand_visible_user_run(msgs, index)
            firsts.add(start)
            index = end + 1
            continue
        index += 1
    return firsts


def compute_edit_fork_gates(
    messages: list[Any],
    *,
    enabled: bool,
    task_created_ats: list[str] | None = None,
    available_boundary_turn_ids: set[str] | None = None,
) -> dict[int, bool]:
    """为每条可见用户消息计算 can_edit_fork（键 = 原始转录下标）。

    在原始转录（含 ui_visible=False 的内部消息）上单遍行走：
    - ``dispatched_prefix``：此前已出现核实派发（assistant metadata.task_ids 非空）；
    - ``pending_users`` 回溯关门：出现派发时，尚未被用户轮回复关闭的 user 消息
      全部置 False —— 覆盖"任务在被点击消息自己的回复轮中创建"的严格判定；
    - 内部轮（heartbeat/cron）的可见回复不清组、不阻断回溯；
    - legacy 兜底：整份转录无 task_ids 字段时，用任务 created_at 与
      "关闭该消息所在 run 的首条 assistant 回复时间戳"比较。

    最终 gate = 任务门槛 && run 首条 && 边界快照可用（或截断到会话开头）。
    """
    msgs = list(messages or [])
    gates: dict[int, bool] = {}
    if not enabled or not msgs:
        return gates
    user_gate: dict[int, bool] = {}
    pending_users: list[int] = []
    dispatched_prefix = False
    seen_task_ids_field = False
    for index, raw in enumerate(msgs):
        if not isinstance(raw, dict):
            continue
        role = message_role(raw)
        if role == "user" and is_visible_user_message(raw):
            user_gate[index] = not dispatched_prefix
            pending_users.append(index)
            continue
        if role == "assistant":
            if "task_ids" in message_metadata(raw):
                seen_task_ids_field = True
            if transcript_task_ids(raw):
                dispatched_prefix = True
                for pending_index in pending_users:
                    user_gate[pending_index] = False
            if not is_internal_assistant_message(raw):
                pending_users = []
    created_ats = sorted(
        str(item or "").strip() for item in list(task_created_ats or []) if str(item or "").strip()
    )
    if created_ats and not seen_task_ids_field:
        for index in list(user_gate):
            if not user_gate[index]:
                continue
            reply_ts = ""
            cursor = index
            while cursor < len(msgs) and is_visible_user_message(msgs[cursor]):
                cursor += 1
            while cursor < len(msgs):
                if message_role(msgs[cursor]) == "assistant":
                    reply_ts = str((msgs[cursor] or {}).get("timestamp") or "").strip()
                    break
                cursor += 1
            if not reply_ts:
                reply_ts = str((msgs[index] or {}).get("timestamp") or "").strip()
            if reply_ts and any(item <= reply_ts for item in created_ats):
                user_gate[index] = False
    run_firsts = visible_user_run_first_indices(msgs)
    for index, allowed in user_gate.items():
        if not allowed or index not in run_firsts:
            gates[index] = False
            continue
        if index == 0:
            gates[index] = True
            continue
        prev_turn = message_turn_id(msgs[index - 1])
        if not prev_turn:
            gates[index] = False
            continue
        if available_boundary_turn_ids is None:
            gates[index] = True
            continue
        gates[index] = prev_turn in available_boundary_turn_ids
    return gates


def _empty_continuity_payload() -> dict[str, Any]:
    return {
        "frontdoor_request_body_messages": [],
        "frontdoor_history_shrink_reason": USER_EDIT_TRUNCATION_REASON,
        "frontdoor_token_preflight_diagnostics": {},
        "frontdoor_actual_request_path": "",
        "frontdoor_actual_request_history": [],
        "frontdoor_stage_state": {},
        "frontdoor_canonical_context": {},
        "compression_state": {},
        "semantic_context_state": {},
        "hydrated_tool_names": [],
        "capability_snapshot_exposure_revision": "",
        "visible_tool_ids": [],
        "visible_skill_ids": [],
        "provider_tool_schema_names": [],
        "frontdoor_restore_source": "none",
        "frontdoor_baseline_sync_decision": "",
        "source_reason": USER_EDIT_TRUNCATION_REASON,
    }


def reconstruct_continuity_before_boundary(
    session_id: str,
    resolution: BoundaryResolution,
) -> tuple[dict[str, Any] | None, str]:
    """构造"截止 boundary 之前"的连续性载荷。

    返回 ``(payload, source)``，source ∈ {"turn_boundary", "fresh_path"}；
    payload 为 None 表示 prev_turn 边界快照缺失（防御分支——调用方应已用
    ``available_boundary_turn_ids`` 预检过资格）。
    """
    if resolution.boundary_index == 0 or not resolution.prev_turn_id:
        return _empty_continuity_payload(), "fresh_path"
    snapshot = read_turn_boundary_snapshot(session_id, resolution.prev_turn_id)
    if not isinstance(snapshot, dict) or not snapshot:
        return None, ""
    payload = dict(snapshot)
    payload["frontdoor_history_shrink_reason"] = USER_EDIT_TRUNCATION_REASON
    payload["source_reason"] = USER_EDIT_TRUNCATION_REASON
    return payload, "turn_boundary"


def delete_actual_request_artifacts_for_turns(session_id: str, removed_turn_ids: list[str]) -> int:
    """删除属于被截断轮的 actual-request artifact。

    空基线截断（fresh_path）后 continuity sidecar 无法阻断重启恢复链落到
    artifact 兜底，不删除会让 ``_restore_frontdoor_state_from_latest_actual_request_artifact``
    复活被删轮内容；非空基线场景同样删除（取证卫生）。
    """
    key = str(session_id or "").strip()
    targets = {str(item or "").strip() for item in list(removed_turn_ids or []) if str(item or "").strip()}
    if not key or not targets:
        return 0
    directory = actual_request_dir_for_session(key, create=False)
    if not directory.exists():
        return 0
    deleted = 0
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("turn_id") or "").strip() in targets:
            try:
                path.unlink()
                deleted += 1
            except Exception:
                continue
    return deleted


def legacy_task_created_ats(agent: Any, session_id: str, messages: list[Any]) -> list[str] | None:
    """legacy 转录（无 task_ids 字段）的任务创建时间兜底，否则返回 None。"""
    if transcript_has_task_ids_field(messages):
        return None
    service = getattr(agent, "main_task_service", None)
    lister = getattr(service, "list_tasks_for_session", None)
    if not callable(lister):
        return None
    try:
        tasks = list(lister(session_id) or [])
    except Exception as exc:
        logger.warning("list_tasks_for_session failed for {}: {}", session_id, exc)
        return None
    return sorted(
        str(getattr(task, "created_at", "") or "").strip()
        for task in tasks
        if str(getattr(task, "created_at", "") or "").strip()
    )


def _recompute_session_counters(session: Any, messages: list[Any]) -> None:
    """截断/复制后重算 metadata 行的派生字段（仅 SessionManager 内部消费）。"""
    last_user_ts: str | None = None
    user_count = 0
    for raw in messages:
        if message_role(raw) != "user":
            continue
        user_count += 1
        timestamp = str((raw or {}).get("timestamp") or "").strip()
        if timestamp:
            last_user_ts = timestamp
    session.last_user_turn_at = last_user_ts
    session.commit_turn_counter = user_count


def _visible_preview_text(messages: list[Any]) -> str:
    for raw in reversed(list(messages or [])):
        if not isinstance(raw, dict):
            continue
        metadata = message_metadata(raw)
        if metadata.get("ui_visible") is False:
            continue
        if is_internal_ceo_user_message(raw):
            continue
        if message_role(raw) not in {"user", "assistant"}:
            continue
        raw_text = metadata.get("web_ceo_raw_text")
        if message_role(raw) == "user" and isinstance(raw_text, str) and isinstance(metadata.get("web_ceo_uploads"), list):
            text = raw_text.strip()
        else:
            text = message_text(raw)
        if text:
            return summarize_preview_text(text)
    return ""


def truncate_web_ceo_session_history(
    *,
    session_manager: Any,
    runtime_manager: Any,
    agent: Any | None = None,
    session_id: str,
    turn_id: str,
) -> dict[str, Any]:
    """把会话截断到被点击用户消息（run 首条）之前，并重建连续性状态。

    调用方必须已持有该会话的 ``_turn_lock``（或确认无 live 会话对象）。
    只支持路线 C（prev_turn 边界快照）与会话开头 fresh_path；快照缺失抛
    ``HistoryEditError``，不做启发式重建。
    """
    key = str(session_id or "").strip()
    session = session_manager.get_or_create(key)
    messages = list(getattr(session, "messages", []) or [])
    available = list_turn_boundary_snapshot_turn_ids(key)
    resolution = resolve_truncation_boundary(messages, turn_id, available_boundary_turn_ids=available)
    if resolution is None:
        raise HistoryEditError("turn_not_found", status_code=404)
    if not resolution.eligible:
        raise HistoryEditError(resolution.reason or "turn_not_editable", status_code=409)
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        task_created_ats=legacy_task_created_ats(agent, key, messages),
        available_boundary_turn_ids=available,
    )
    if not gates.get(resolution.boundary_index, False):
        raise HistoryEditError("edit_fork_blocked_by_async_task", status_code=409)
    payload, source = reconstruct_continuity_before_boundary(key, resolution)
    if payload is None:
        # 资格校验与执行之间边界快照被修剪/清理（防御分支）。
        raise HistoryEditError("boundary_unavailable", status_code=409)

    boundary_index = resolution.boundary_index
    removed = [dict(item) if isinstance(item, dict) else item for item in messages[boundary_index:]]
    removed_turn_ids = list(resolution.removed_turn_ids)

    # 1) 转录截断：结构编辑触发 SessionManager 原子全量重写。
    del session.messages[boundary_index:]
    remaining = list(session.messages)
    _recompute_session_counters(session, remaining)
    metadata = dict(getattr(session, "metadata", {}) or {})
    metadata["last_preview_text"] = _visible_preview_text(remaining)
    session.metadata = metadata
    session.updated_at = datetime.now()
    session_manager.save(session)

    # 2) 连续性 sidecar 替换 + 恢复链上下游清理。
    if source == "fresh_path":
        # 空基线 sidecar 挡不住重启 artifact 兜底：整目录清除。
        clear_actual_request_history(key)
    else:
        delete_actual_request_artifacts_for_turns(key, removed_turn_ids)
    write_completed_continuity_snapshot(key, payload)
    clear_inflight_turn_snapshot(key)
    clear_paused_execution_context(key)
    clear_turn_boundary_snapshots(key, removed_turn_ids)

    # 3) 内存会话对象就地变更（旧 WS 闭包持同一引用，避免陈旧基线竞态）。
    runtime_session = None
    getter = getattr(runtime_manager, "get", None)
    if callable(getter):
        try:
            runtime_session = getter(key)
        except Exception:
            runtime_session = None
    if runtime_session is not None:
        applier = getattr(runtime_session, "apply_history_truncation_state", None)
        if callable(applier):
            applier(payload, removed_turn_ids=removed_turn_ids)

    logger.info(
        "Truncated web CEO session {} at turn {} ({} message(s) removed, continuity={})",
        key,
        turn_id,
        len(removed),
        source,
    )
    return {
        "session_id": key,
        "boundary_turn_id": str(turn_id or "").strip(),
        "removed_message_count": len(removed),
        "removed_turn_ids": removed_turn_ids,
        "continuity_source": source,
    }


def _guess_mime_type(name: str, fallback: str | None = None) -> str:
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _upload_kind(*, mime_type: str, name: str) -> str:
    if str(mime_type or "").lower().startswith("image/"):
        return "image"
    guessed, _ = mimetypes.guess_type(name)
    if isinstance(guessed, str) and guessed.lower().startswith("image/"):
        return "image"
    return "file"


def _rewrite_paths_in_value(value: Any, replacements: dict[str, str]) -> Any:
    if not replacements:
        return value
    if isinstance(value, str):
        text = value
        for src, dst in replacements.items():
            if src and src in text:
                text = text.replace(src, dst)
        return text
    if isinstance(value, dict):
        return {key: _rewrite_paths_in_value(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths_in_value(item, replacements) for item in value]
    return value


def fork_web_ceo_session(
    *,
    session_manager: Any,
    agent: Any | None = None,
    session_id: str,
    turn_id: str,
    title: str | None = None,
) -> dict[str, Any]:
    """把会话在 boundary 之前的前缀复制成新会话，返回 composer 预填载荷。

    对源会话只读（可在源轮运行中执行）；复制内容 = 转录前缀（附件文件复制 +
    描述符/内容路径重写）+ 截断态连续性 sidecar + prev_turn 边界快照。
    被点击消息本身不进新转录，其原文与（复制后的）附件作为 composer 预填返回。
    """
    key = str(session_id or "").strip()
    source_session = session_manager.get_or_create(key)
    messages = list(getattr(source_session, "messages", []) or [])
    available = list_turn_boundary_snapshot_turn_ids(key)
    resolution = resolve_truncation_boundary(messages, turn_id, available_boundary_turn_ids=available)
    if resolution is None:
        raise HistoryEditError("turn_not_found", status_code=404)
    if not resolution.eligible:
        raise HistoryEditError(resolution.reason or "turn_not_editable", status_code=409)
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        task_created_ats=legacy_task_created_ats(agent, key, messages),
        available_boundary_turn_ids=available,
    )
    if not gates.get(resolution.boundary_index, False):
        raise HistoryEditError("edit_fork_blocked_by_async_task", status_code=409)
    # 先把边界快照读进内存：源会话若在跑，后续修剪/覆盖不影响本次 fork。
    payload, source = reconstruct_continuity_before_boundary(key, resolution)
    if payload is None:
        raise HistoryEditError("boundary_unavailable", status_code=409)

    boundary_index = resolution.boundary_index
    clicked = resolution.boundary_message
    clicked_metadata = message_metadata(clicked)

    new_key = new_web_ceo_session_id()
    new_session = session_manager.get_or_create(new_key)
    ensure_ceo_session_metadata(new_session)
    dst_upload_dir = upload_dir_for_session(new_key)
    path_map: dict[str, dict[str, Any]] = {}

    def copy_upload_descriptor(descriptor: Any) -> dict[str, Any] | None:
        if not isinstance(descriptor, dict):
            return None
        src = str(descriptor.get("path") or "").strip()
        if not src:
            return None
        if src in path_map:
            return dict(path_map[src])
        src_path = Path(src)
        if not src_path.exists() or not src_path.is_file():
            logger.warning("Fork upload source missing, descriptor dropped: {}", src)
            return None
        name = safe_filename(str(descriptor.get("name") or src_path.name).strip()) or "upload.bin"
        target = dst_upload_dir / f"{uuid.uuid4().hex[:12]}_{name}"
        try:
            shutil.copy2(src_path, target)
        except Exception as exc:
            logger.warning("Fork upload copy failed for {}: {}", src, exc)
            return None
        resolved = target.resolve()
        mime_type = _guess_mime_type(name, str(descriptor.get("mime_type") or ""))
        try:
            relative_path = resolved.relative_to(workspace_path()).as_posix()
        except Exception:
            relative_path = resolved.as_posix()
        new_descriptor = {
            "name": name,
            "path": str(resolved),
            "relative_path": relative_path,
            "mime_type": mime_type,
            "size": resolved.stat().st_size,
            "kind": _upload_kind(mime_type=mime_type, name=name),
        }
        path_map[src] = new_descriptor
        return dict(new_descriptor)

    # 被点击消息的附件先复制（composer 预填用，不进转录）。
    composer_uploads: list[dict[str, Any]] = []
    for descriptor in list(clicked_metadata.get("web_ceo_uploads") or []):
        copied_descriptor = copy_upload_descriptor(descriptor)
        if copied_descriptor:
            composer_uploads.append(copied_descriptor)

    copied_prefix: list[dict[str, Any]] = []
    for raw in messages[:boundary_index]:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
        if metadata is not None:
            uploads = metadata.get("web_ceo_uploads")
            if isinstance(uploads, list) and uploads:
                new_uploads = []
                for descriptor in uploads:
                    copied_descriptor = copy_upload_descriptor(descriptor)
                    if copied_descriptor:
                        new_uploads.append(copied_descriptor)
                if new_uploads:
                    metadata["web_ceo_uploads"] = new_uploads
                else:
                    metadata.pop("web_ceo_uploads", None)
        attachments = item.get("attachments")
        if isinstance(attachments, list) and attachments:
            item["attachments"] = [
                str(path_map.get(str(entry or "").strip(), {}).get("path") or entry)
                for entry in attachments
            ]
        content = item.get("content")
        if isinstance(content, str) and path_map:
            item["content"] = _rewrite_paths_in_value(
                content, {src: str(dst.get("path") or "") for src, dst in path_map.items()}
            )
        copied_prefix.append(item)

    new_session.messages.extend(copied_prefix)
    _recompute_session_counters(new_session, copied_prefix)
    source_title = str((getattr(source_session, "metadata", {}) or {}).get("title") or "").strip() or DEFAULT_CEO_SESSION_TITLE
    fork_title = str(title or "").strip() or f"{source_title} · Fork"
    new_metadata = dict(getattr(new_session, "metadata", {}) or {})
    new_metadata["title"] = fork_title
    new_metadata["last_preview_text"] = _visible_preview_text(copied_prefix)
    new_session.metadata = new_metadata
    new_session.updated_at = datetime.now()
    session_manager.save(new_session)

    # 连续性载荷：上传路径重写后写入新会话；不跨会话引用源 artifact。
    replacements = {src: str(dst.get("path") or "") for src, dst in path_map.items()}
    fork_payload = _rewrite_paths_in_value(copy.deepcopy(payload), replacements)
    fork_payload["frontdoor_actual_request_path"] = ""
    fork_payload["frontdoor_actual_request_history"] = []
    fork_payload["frontdoor_history_shrink_reason"] = USER_EDIT_TRUNCATION_REASON
    fork_payload["source_reason"] = USER_EDIT_TRUNCATION_REASON
    write_completed_continuity_snapshot(new_key, fork_payload)
    if source == "turn_boundary" and resolution.prev_turn_id:
        # 复制 prev_turn 边界快照，让新会话尾部同样具备再编辑/再 Fork 资格。
        write_turn_boundary_snapshot(new_key, resolution.prev_turn_id, fork_payload)

    composer_raw_text = clicked_metadata.get("web_ceo_raw_text")
    if isinstance(composer_raw_text, str) and isinstance(clicked_metadata.get("web_ceo_uploads"), list):
        composer_text = composer_raw_text
    else:
        composer_text = message_text(clicked)

    logger.info(
        "Forked web CEO session {} at turn {} into {} ({} message(s) copied, continuity={})",
        key,
        turn_id,
        new_key,
        len(copied_prefix),
        source,
    )
    return {
        "session_id": new_key,
        "source_session_id": key,
        "copied_message_count": len(copied_prefix),
        "continuity_source": source,
        "composer": {
            "text": str(composer_text or ""),
            "uploads": composer_uploads,
        },
    }
