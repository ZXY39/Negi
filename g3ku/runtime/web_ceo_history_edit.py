"""Web CEO 会话历史截断与 Fork（用户消息编辑重发 / Fork 会话）。

本模块是「编辑重发 / Fork」的服务端核心：

- 截断数据源 = 每轮边界快照（``.g3ku/web-ceo-turn-boundaries/<session>/<turn_id>.json.gz``，
  由 ``RuntimeAgentSession._sync_completed_continuity_snapshot`` 每个用户轮 upsert，
  只保留最近 ``TURN_BOUNDARY_SNAPSHOT_KEEP`` 份；心跳/cron 内部轮不写、不占名额）。
  不做任何启发式基线重建：锚点轮的快照缺失即视为不合格（前端不显示按钮、端点 409）。
- 截断点必须落在干净的轮边界上：被点击消息必须是其所在 user-run
  （极大连续可见 user 消息段）的首条；批次兄弟与中途消费的 follow-up
  没有自己的轮边界，不显示按钮。
- 异步任务门槛：只有当**落在被截断区间里的首次派发**仍未完成（任务 ``in_progress``，
  含树被暂停的任务）时才不可编辑/Fork。转录行 ``metadata.task_ids`` 的首次出现即派发点，
  因此心跳回复顺口提到的任务号、暂停/补充归档行重新盖上的任务号都不算新派发；
  边界之前建立的任务记录留在前缀里，截断不动它。legacy 转录（整份无 task_ids 字段）
  用未完成任务的 created_at 与被点击消息的发送时间比较兜底。

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
    read_completed_continuity_snapshot,
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


def task_dispatch_first_indices(messages: list[Any]) -> dict[str, int]:
    """每个任务号在转录中「首次作为派发记录出现」的下标。

    只在 assistant 行上收集，且保留最早一次：心跳回复顺口提到的任务号、暂停归档与
    待发送补充归档行重新盖上的任务号，都在更早的派发行里出现过，因此不会把回声
    当成新的建立点。
    """
    first: dict[str, int] = {}
    for index, raw in enumerate(list(messages or [])):
        if message_role(raw) != "assistant":
            continue
        for task_id in transcript_task_ids(raw):
            first.setdefault(task_id, index)
    return first


def session_unfinished_task_ids(agent: Any, session_id: str) -> set[str] | None:
    """本会话仍未完成的任务号（任务 ``in_progress``，含执行树被暂停的任务）。

    读不到任务服务时返回 None，调用方按「仍活」保守处理。
    """
    service = getattr(agent, "main_task_service", None)
    lister = getattr(service, "list_unfinished_tasks_for_session", None)
    if not callable(lister):
        return None
    try:
        tasks = list(lister(session_id) or [])
    except Exception as exc:
        logger.warning("list_unfinished_tasks_for_session failed for {}: {}", session_id, exc)
        return None
    return {str(getattr(task, "task_id", "") or "").strip() for task in tasks} - {""}


def _is_internal_transcript_row(message: Any) -> bool:
    """内部轮留下的行：心跳/cron 回复、内部 prompt 的 system/user 行。"""
    role = message_role(message)
    if role == "assistant":
        return is_internal_assistant_message(message)
    if role == "user":
        return not is_visible_user_message(message)
    return role == "system"


def boundary_anchor_turn_id(messages: list[Any], index: int) -> str:
    """被点击 user-run 首条 ``index`` 的截断基线来源轮次。

    从上一条行往回走，跨过内部轮的行（它们不写边界快照）；遇到可见用户行或普通
    助手行就交回它的轮次——那个轮的快照不在盘上时就是真出了保留窗口。归档行
    （``follow_up_archive`` 的复合 turn_id 从不落盘）按它归档的轮次解包。
    """
    msgs = list(messages or [])
    cursor = index - 1
    while cursor >= 0:
        raw = msgs[cursor]
        if not isinstance(raw, dict):
            cursor -= 1
            continue
        if _is_internal_transcript_row(raw):
            cursor -= 1
            continue
        archived = str(message_metadata(raw).get("archived_from_turn_id") or "").strip()
        return archived or message_turn_id(raw)
    return ""


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
    - ``boundary_unavailable``：锚点轮的边界快照缺失（超出保留窗口或旧数据）。
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
    prev_turn_id = boundary_anchor_turn_id(msgs, run_start) if run_start > 0 else ""
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
    unfinished_task_ids: set[str] | None = None,
) -> dict[int, bool]:
    """为每条可见用户消息计算编辑/Fork 资格（键 = 原始转录下标）。

    三条判据，任一不过即 False：
    - 任务门槛：截断删掉的是 ``[该消息, 转录末尾]``，所以只有「首次派发落在这个区间
      里、且该任务仍未完成」才拦。边界之前建立的任务，其派发记录留在保留前缀里，
      不受截断影响。``unfinished_task_ids`` 为 None（读不到任务服务）时按仍未完成
      保守处理。整份转录没有 task_ids 字段时走 legacy：用仍未完成任务的 created_at
      与该条消息的发送时间比较。
    - run 首条：一个 user-run 只有一个干净边界。
    - 边界快照：锚点轮（跨过内部轮往回找，见 ``boundary_anchor_turn_id``）的快照还在
      盘上，或截断边界就是会话开头。
    """
    msgs = list(messages or [])
    gates: dict[int, bool] = {}
    if not enabled or not msgs:
        return gates
    run_firsts = visible_user_run_first_indices(msgs)
    first_dispatch = task_dispatch_first_indices(msgs)
    seen_task_ids_field = any(
        isinstance(raw, dict) and "task_ids" in message_metadata(raw) for raw in msgs
    )
    unfinished_known = unfinished_task_ids is not None
    unfinished = unfinished_task_ids or set()
    created_ats = sorted(
        str(item or "").strip() for item in list(task_created_ats or []) if str(item or "").strip()
    )
    for index, raw in enumerate(msgs):
        if not isinstance(raw, dict) or not is_visible_user_message(raw):
            continue
        if index not in run_firsts:
            gates[index] = False
            continue
        if seen_task_ids_field:
            blocked_by_task = any(
                start >= index and (task_id in unfinished if unfinished_known else True)
                for task_id, start in first_dispatch.items()
            )
        elif created_ats:
            sent_ts = str(raw.get("timestamp") or "").strip()
            blocked_by_task = not sent_ts or any(item >= sent_ts for item in created_ats)
        else:
            blocked_by_task = False
        if blocked_by_task:
            gates[index] = False
            continue
        if index == 0:
            gates[index] = True
            continue
        anchor = boundary_anchor_turn_id(msgs, index)
        if not anchor:
            gates[index] = False
            continue
        if available_boundary_turn_ids is None:
            gates[index] = True
            continue
        gates[index] = anchor in available_boundary_turn_ids
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
    """legacy 转录（整份无 task_ids 字段）的兜底：仍未完成任务的创建时间。

    读不到未完成清单时退回全部任务——宁可多拦，不可把仍在跑的任务截掉。
    """
    if transcript_has_task_ids_field(messages):
        return None
    service = getattr(agent, "main_task_service", None)
    lister = getattr(service, "list_unfinished_tasks_for_session", None)
    if not callable(lister):
        lister = getattr(service, "list_tasks_for_session", None)
    if not callable(lister):
        return None
    try:
        tasks = list(lister(session_id) or [])
    except Exception as exc:
        logger.warning("legacy task listing failed for {}: {}", session_id, exc)
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
        unfinished_task_ids=session_unfinished_task_ids(agent, key),
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


def reply_fork_target(messages: list[Any], turn_id: str) -> tuple[int, str] | None:
    """把"在某条模型回复之后分叉"翻译成既有的边界模型。

    返回 ``(该轮最后一条可见助手行的下标, 其后第一条可见用户消息的 turn_id)``。
    turn_id 非空时切点与"在那条提问之前分叉"完全相同——前缀、连续性锚点、任务门槛、
    保留窗口全都复用一条路径，不会出现"新会话的基线提到了它转录里没有的事"。
    turn_id 为空表示这条回复就是转录尾部（尾部 Fork：整份转录都留，输入框留空）。
    该轮找不到可见助手行（纯提问轮、被裁掉的内部轮）时返回 None。
    """
    normalized_turn_id = str(turn_id or "").strip()
    msgs = list(messages or [])
    if not normalized_turn_id:
        return None
    reply_index = -1
    for index, raw in enumerate(msgs):
        if not isinstance(raw, dict) or message_role(raw) != "assistant":
            continue
        if message_turn_id(raw) != normalized_turn_id:
            continue
        if _is_internal_transcript_row(raw) or is_internal_assistant_message(raw):
            continue
        if not message_text(raw):
            continue
        reply_index = index
    if reply_index < 0:
        return None
    for raw in msgs[reply_index + 1:]:
        if isinstance(raw, dict) and is_visible_user_message(raw):
            return reply_index, message_turn_id(raw)
    return reply_index, ""


def _is_visible_reply_row(message: Any) -> bool:
    if not isinstance(message, dict) or message_role(message) != "assistant":
        return False
    if _is_internal_transcript_row(message) or is_internal_assistant_message(message):
        return False
    if message_metadata(message).get("silent_reply") is True:
        return False
    return bool(message_text(message))


def _visible_reply_turn_id_at(messages: list[Any], index: int) -> str:
    raw = messages[index] if 0 <= index < len(messages) else None
    if not _is_visible_reply_row(raw):
        return ""
    return message_turn_id(raw)


def compute_reply_fork_turn_ids(
    messages: list[Any],
    fork_gates: dict[int, bool] | None,
) -> set[str]:
    """回复行 Fork 的合格轮次集合（与 ``reply_fork_target`` 同一套切点判据）。

    两条来源：
    - 某条可见用户消息有 Fork 资格 ⇒ 它上面那条可见回复同样有（两者是同一个切点，
      判据、保留窗口、任务门槛全部继承，不会出现"按物理下标切"与快照不一致）；
    - 转录尾部那条可见回复（它之后没有提问）⇒ 永远合格，走尾部分支，不截任何东西。
    """
    msgs = list(messages or [])
    turn_ids: set[str] = set()
    for index in sorted(fork_gates or {}):
        if not (fork_gates or {}).get(index):
            continue
        cursor = index - 1
        while cursor >= 0:
            turn_id = _visible_reply_turn_id_at(msgs, cursor)
            if turn_id:
                turn_ids.add(turn_id)
                break
            cursor -= 1
    last_reply_index = -1
    for index, raw in enumerate(msgs):
        if _is_visible_reply_row(raw):
            last_reply_index = index
    if last_reply_index >= 0:
        following_user = any(
            is_visible_user_message(raw) for raw in msgs[last_reply_index + 1:]
        )
        if not following_user:
            turn_id = _visible_reply_turn_id_at(msgs, last_reply_index)
            if turn_id:
                turn_ids.add(turn_id)
    return turn_ids


def fork_web_ceo_session(
    *,
    session_manager: Any,
    agent: Any | None = None,
    session_id: str,
    turn_id: str,
    title: str | None = None,
    at: str = "question",
) -> dict[str, Any]:
    """把会话在 boundary 之前的前缀复制成新会话，返回 composer 预填载荷。

    对源会话只读（可在源轮运行中执行）；复制内容 = 转录前缀（附件文件复制 +
    描述符/内容路径重写）+ 截断态连续性 sidecar + prev_turn 边界快照。
    被点击消息本身不进新转录，其原文与（复制后的）附件作为 composer 预填返回。

    ``at="reply"`` 时 ``turn_id`` 指**模型回复**所在的轮次：切点翻译成该回复之后
    第一条提问之前（与 ``at="question"`` 共用全部判据）；那条回复已是转录尾部时
    走尾部分支——整份转录都进新会话、连续性取源会话当前的 completed sidecar、
    composer 留空，所以最后一条回复也有 Fork 入口，不必等下一条提问发出来。
    """
    key = str(session_id or "").strip()
    source_session = session_manager.get_or_create(key)
    messages = list(getattr(source_session, "messages", []) or [])
    normalized_mode = str(at or "question").strip().lower()
    tail_fork = False
    if normalized_mode == "reply":
        target = reply_fork_target(messages, turn_id)
        if target is None:
            raise HistoryEditError("turn_not_found", status_code=404)
        _reply_index, next_user_turn_id = target
        if next_user_turn_id:
            turn_id = next_user_turn_id
        else:
            tail_fork = True
    available = list_turn_boundary_snapshot_turn_ids(key)
    if tail_fork:
        # 尾部 Fork 不截任何东西：任务门槛与保留窗口都不适用。
        resolution = BoundaryResolution(boundary_index=len(messages), eligible=True, reason="")
        payload = read_completed_continuity_snapshot(key)
        if not isinstance(payload, dict) or not payload:
            payload = _empty_continuity_payload()
        source = "session_continuity"
    else:
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
            unfinished_task_ids=session_unfinished_task_ids(agent, key),
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
        "Forked web CEO session {} at turn {} (at={}, into {}, {} message(s) copied, continuity={})",
        key,
        turn_id,
        "reply" if normalized_mode == "reply" else "question",
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
