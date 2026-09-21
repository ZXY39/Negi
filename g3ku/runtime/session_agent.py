from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.core.events import AgentEvent
from g3ku.core.messages import AssistantMessage, UserInputMessage
from g3ku.core.results import RunResult
from g3ku.core.state import AgentState, StructuredError
from g3ku.prompt_trace import render_output_trace
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.frontdoor.canonical_context import (
    TRANSCRIPT_PROJECTION_MODE,
    canonical_context_tool_items,
    default_frontdoor_canonical_context,
    materialize_transcript_view,
    normalize_frontdoor_canonical_context,
    plan_transcript_cc_row,
    project_canonical_context_for_transcript,
    repair_transcript_cc_chain,
)
from g3ku.runtime.frontdoor.message_builder import (
    MEMORY_SNAPSHOT_ADOPTION_TOKEN_COMPRESSION,
    adopt_memory_snapshot,
)
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted
from g3ku.runtime.reply_tokens import is_silent_reply_token
from main.runtime.stage_budget import STAGE_TURN_END_SUMMARY_POINTER

_CONTROL_TOOL_NAMES = {"stop_tool_execution"}
_LEGACY_CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}
_TRANSCRIPT_TURN_ID_KEY = "_transcript_turn_id"
_TRANSCRIPT_BATCH_ID_KEY = "_transcript_batch_id"
_TRANSCRIPT_STATE_KEY = "_transcript_state"
_TRANSCRIPT_STATE_PENDING = "pending"
_TRANSCRIPT_STATE_PAUSED = "paused"
_TRANSCRIPT_STATE_COMPLETED = "completed"
# 回合失败时把本轮已落盘的内部提示词（心跳/cron 的规则 system + 事件束 user）翻成
# discarded，使其退出后续可重放上下文。这些消息在模型调用前就写盘（见
# _persist_internal_prompt_messages），失败回合若不回收会在每一轮请求体里反复堆积
# （实测单会话累积到占上下文 38.8%）。原始 jsonl 行保留，仅改 metadata 状态。
_TRANSCRIPT_STATE_DISCARDED = "discarded"
# 手动上下文压缩的运行态：由 api/ceo_sessions 写、由 frontdoor_inbound_hold() 读。
# 常量放在运行层，是为了让四条入站车道问的是同一个字段名（api 层依赖运行层，反向不行）。
MANUAL_COMPRESSION_STATE_ATTR = "_manual_context_compression"
MANUAL_COMPRESSION_RUNNING = "running"
# _internal_prompt_message_metadata 写入的内部提示词种类；翻转 discarded 时按此匹配，
# 避免误伤同 turn 的助手错误行（其 metadata 无 internal_prompt_kind）。
_INTERNAL_PROMPT_KINDS = frozenset(
    {"heartbeat_rule", "heartbeat_event_bundle", "cron_rule", "cron_event_bundle"}
)
_TASK_ID_PATTERN = re.compile(r"task:[A-Za-z0-9][\w:-]*")
_ASSISTANT_STREAM_FLUSH_WINDOW_SECONDS = 0.075
# 上下文压缩区分线：转录里一条 UI-only 的 system 行，永久标出「已折叠的历史」与
# 「之后新回合」的分界。手动压缩由 API 层写，自动（发送时）压缩由回合收尾写。
CONTEXT_COMPRESSION_MARKER_KIND = "context_compression"
CONTEXT_COMPRESSION_MARKER_LABELS = {
    "completed": "会话已压缩",
    "paused": "压缩已暂停",
}


def _project_transcript_canonical_context(canonical_context: Any) -> dict[str, Any]:
    if not isinstance(canonical_context, dict) or not canonical_context:
        return {}
    return project_canonical_context_for_transcript(canonical_context)


# 回合 finalize 尾部（转录落盘 → 记忆复核簿记 → 终态 emit）的慢阈值。正常情况
# 这一段全是本地 I/O 与队列推送，远低于 1 秒；超过阈值说明某个 await 异常。
# 2026-09 的 cron 派发挂起事故里挂起点只能靠外部看门狗 dump await 链定位，
# 这里补上「慢但最终返回了」场景的分阶段证据。
_TURN_TAIL_SLOW_WARN_SECONDS = 30.0


class _TurnTailProfiler:
    """Stage timer for the post-model finalize tail of a completed turn.

    Each ``mark`` closes one named stage; ``warn_if_slow`` logs a per-stage
    breakdown when the whole tail exceeds the threshold. A stage that never
    completes (a wedged await) is what the cron dispatch watchdog and the
    bridge slow-prompt watchdog dump live stacks for — this profiler covers
    the complementary "slow but eventually returned" case, so maintainers can
    tell transcript persistence, memory-review bookkeeping and the terminal
    emit chain apart without guessing.
    """

    def __init__(self, *, session_key: str, threshold_s: float = _TURN_TAIL_SLOW_WARN_SECONDS) -> None:
        self._session_key = str(session_key or "")
        self._threshold_s = float(threshold_s)
        self._started = time.monotonic()
        self._last = self._started
        self._stages: list[tuple[str, float]] = []

    def mark(self, stage: str) -> None:
        now = time.monotonic()
        self._stages.append((stage, now - self._last))
        self._last = now

    def warn_if_slow(self) -> None:
        total = time.monotonic() - self._started
        if total < self._threshold_s:
            return
        breakdown = (
            " | ".join(f"{name}={seconds:.1f}s" for name, seconds in self._stages)
            or "<no stage completed>"
        )
        logger.warning(
            "turn finalize tail slow for session {}: total={:.1f}s (threshold {:.0f}s) | {}",
            self._session_key,
            total,
            self._threshold_s,
            breakdown,
        )


# User-facing message shown when a turn fails. The raw exception text is kept
# for operators (error file / transcript metadata / "error" event) but never
# surfaced to channel users verbatim.
TURN_FAILED_FRIENDLY_TEXT = (
    "这一轮处理没有完成，请稍后重试；如果反复出现，请查看任务面板或错误日志了解详情。"
)

# Session-key namespaces that run the CEO frontdoor and therefore participate in
# the persisted frontdoor continuity lifecycle (completed-continuity snapshot
# write + restore). This used to be web-only, which meant a channel session
# (china:*) lost its entire baseline on process restart; the first post-restart
# internal (cron/heartbeat) turn then rebuilt the prompt with no base system
# prompt. Continuity must apply to every frontdoor session regardless of the
# channel it arrived on.
_FRONTDOOR_CONTINUITY_SESSION_KEY_PREFIXES = ("web:", "china:", "cron:", "ext:")


def _frontdoor_continuity_session_key(session_key: str) -> bool:
    key = str(session_key or "").strip()
    return any(
        key.startswith(prefix) for prefix in _FRONTDOOR_CONTINUITY_SESSION_KEY_PREFIXES
    )


class RuntimeAgentSession:
    """Primary AgentSession implementation backed by the runtime engine."""

    def __init__(
        self,
        loop,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        memory_channel: str | None = None,
        memory_chat_id: str | None = None,
    ):
        self._loop = loop
        self._channel = channel
        self._chat_id = chat_id
        self._memory_channel = str(memory_channel or channel or "unknown")
        self._memory_chat_id = str(memory_chat_id or chat_id or "unknown")
        self._multi_agent_runner = getattr(loop, "multi_agent_runner", None)
        self._state = AgentState(
            session_key=session_key,
            system_prompt="",
            model=str(getattr(loop, "model", "")),
            reasoning_effort=getattr(loop, "reasoning_effort", None),
        )
        self._listeners: set[Callable[[AgentEvent], Awaitable[None] | None]] = set()
        self._last_prompt: str | UserInputMessage = ""
        self._event_log: list[dict] = []
        self._pending_tool_call_names: dict[str, str] = {}
        self._pending_tool_name_calls: dict[str, deque[str]] = {}
        self._background_tool_targets: dict[str, dict[str, str]] = {}
        self._tool_seq: int = 0
        self._active_cancel_token: ToolCancellationToken | None = None
        self._latest_sidecar_tool_observation: dict[str, Any] = {}
        self._preserved_inflight_turn: dict[str, Any] | None = None
        self._follow_up_transition_snapshot: dict[str, Any] | None = None
        self._paused_execution_context: dict[str, Any] | None = None
        self._frontdoor_stage_state: dict[str, Any] = {}
        self._frontdoor_canonical_context: dict[str, Any] = default_frontdoor_canonical_context()
        self._compression_state: dict[str, Any] = {}
        self._semantic_context_state: dict[str, Any] = {}
        self._frontdoor_hydrated_tool_names: list[str] = []
        self._frontdoor_repair_required_tool_items: list[dict[str, Any]] = []
        self._frontdoor_repair_required_skill_items: list[dict[str, Any]] = []
        self._frontdoor_selection_debug: dict[str, Any] = {}
        self._frontdoor_request_body_messages: list[dict[str, Any]] = []
        self._frontdoor_history_shrink_reason: str = ""
        self._frontdoor_pending_shrink_reason: str = ""
        self._frontdoor_token_preflight_diagnostics: dict[str, Any] = {}
        self._frontdoor_model_retry_status: dict[str, Any] | None = None
        # 本轮是否真实发生过内联 token 压缩（任一请求 applied 即置位，轮首清零）。
        # 只表达“当轮压缩事件”，与跨轮残留的 _frontdoor_history_shrink_reason 不同。
        self._frontdoor_token_compression_applied_turn: bool = False
        # 长期记忆快照的会话级冻结值：注入侧只读它，改写只发生在采纳点
        # （会话首请求 / 压缩轮末 / 手动压缩成功后）。None = 本会话尚未采纳。
        self._frontdoor_memory_snapshot_text: str | None = None
        self._frontdoor_memory_snapshot_adopted_at: str = ""
        self._frontdoor_memory_snapshot_adoption_reason: str = ""
        self._frontdoor_compression_generation_seq: int = 0
        self._active_frontdoor_compression_generation: int | None = None
        self._cancelled_frontdoor_compression_generations: set[int] = set()
        self._frontdoor_actual_request_path: str = ""
        # durable 基线的写入代号：只在唯一前进点 `_persist_frontdoor_actual_request` 递增。
        # 手动压缩拿它做"我读到的基线还是不是当前基线"的判定——渠道回合注册在 None 键上，
        # pause 既停不掉它也等不到它（external_turns 模块 docstring），所以压缩在途时
        # 完全可能有一个用旧种子组装的回合先/后落盘（实盘：17,956 tok 摘要 18 秒后被
        # 115,338 tok 覆盖）。没有这条判定，闸门只能挡住"新起的回合"，挡不住"已经在跑的回合"。
        self._frontdoor_baseline_revision: int = 0
        self._frontdoor_actual_request_history: list[dict[str, Any]] = []
        self._frontdoor_previous_actual_request_path: str = ""
        self._frontdoor_previous_actual_request_history: list[dict[str, Any]] = []
        self._frontdoor_prompt_cache_key_hash: str = ""
        self._frontdoor_actual_request_hash: str = ""
        self._frontdoor_actual_request_message_count: int = 0
        self._frontdoor_actual_tool_schema_hash: str = ""
        self._frontdoor_capability_snapshot_exposure_revision: str = ""
        self._frontdoor_visible_tool_ids: list[str] = []
        self._frontdoor_visible_skill_ids: list[str] = []
        self._frontdoor_provider_tool_schema_names: list[str] = []
        self._frontdoor_restore_source: str = "none"
        self._frontdoor_baseline_sync_decision: str = ""
        self._frontdoor_completed_continuity_bridge_pending: bool = False
        self._last_stop_reason: str = ""
        self._active_turn_id: str | None = None
        # 每轮可见 frontdoor 请求的累计 token 用量（turn_id -> {input_tokens, output_tokens, cache_hit_tokens, call_count}）。
        # 只在内存中累积、O(1) 更新，供气泡下方实时展示，不做磁盘读取。
        self._frontdoor_turn_usage: dict[str, dict[str, int]] = {}
        self._active_batch_id: str | None = None
        self._active_user_batch_inputs: list[UserInputMessage] = []
        self._last_verified_task_ids: list[str] = []
        self._assistant_stream_seq: int = 0
        self._assistant_stream_pending_text: str = ""
        self._assistant_stream_last_emitted_text: str = ""
        self._assistant_stream_last_emit_monotonic: float = 0.0
        self._assistant_stream_flush_task: asyncio.Task[Any] | None = None
        self._assistant_segment_open: bool = False
        self._turn_lock = asyncio.Lock()
        self._restore_frontdoor_persistent_state()
        self._rehydrate_queued_follow_ups()

    @property
    def state(self) -> AgentState:
        return self._state

    def subscribe(self, listener: Callable[[AgentEvent], Awaitable[None] | None]):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def state_dict(self) -> dict:
        data = asdict(self._state)
        data["session_id"] = self._state.session_key
        data["pending_tool_calls"] = sorted(self._state.pending_tool_calls)
        if self._state.last_error is not None:
            data["last_error"] = asdict(self._state.last_error)
        if self._last_stop_reason:
            data["stop_reason"] = self._last_stop_reason
        # 回合外的手动压缩没有 inflight turn 可挂载进度，state 快照是它唯一的实时通道。
        compression = self._compression_snapshot()
        if compression:
            data["compression"] = compression
        return data

    @staticmethod
    def _normalized_name_list(values: Any, *, sort_values: bool = False) -> list[str]:
        normalized: list[str] = []
        for raw in list(values or []):
            name = str(raw or "").strip()
            if not name or name in normalized:
                continue
            normalized.append(name)
        if sort_values:
            return sorted(normalized)
        return normalized

    @staticmethod
    def _restorable_frontdoor_request_body_messages(
        request_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        from g3ku.runtime.frontdoor.tool_contract import is_frontdoor_tool_contract_message
        from g3ku.runtime.web_ceo_sessions import strip_multimodal_blocks_from_message_records
        from main.runtime.stage_messages import is_turn_only_system_note_message

        body_messages: list[dict[str, Any]] = []
        for item in list(request_messages or []):
            if not isinstance(item, dict):
                continue
            record = dict(item)
            if is_frontdoor_tool_contract_message(record):
                continue
            if is_turn_only_system_note_message(record):
                continue
            body_messages.append(record)
        return strip_multimodal_blocks_from_message_records(body_messages)

    @staticmethod
    def _frontdoor_actual_request_history_record_from_payload(
        payload: dict[str, Any],
        *,
        artifact_path: str,
    ) -> dict[str, Any]:
        return {
            "path": str(artifact_path or "").strip(),
            "request_id": str(payload.get("request_id") or "").strip(),
            "created_at": str(payload.get("created_at") or "").strip(),
            "persisted_at": str(payload.get("persisted_at") or "").strip(),
            "turn_id": str(payload.get("turn_id") or "").strip(),
            "actual_request_hash": str(payload.get("actual_request_hash") or "").strip(),
            "actual_request_message_count": int(payload.get("actual_request_message_count") or 0),
            "actual_tool_schema_hash": str(payload.get("actual_tool_schema_hash") or "").strip(),
            "prompt_cache_key_hash": str(payload.get("prompt_cache_key_hash") or "").strip(),
            "provider_model": str(payload.get("provider_model") or "").strip(),
        }

    @classmethod
    def _frontdoor_request_body_match_normalized_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._frontdoor_request_body_match_normalized_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._frontdoor_request_body_match_normalized_value(item) for item in value]
        if isinstance(value, str):
            return value.replace("\r\n", "\n").rstrip()
        return value

    @classmethod
    def _frontdoor_request_body_messages_match(
        cls,
        first: list[dict[str, Any]] | None,
        second: list[dict[str, Any]] | None,
    ) -> bool:
        first_records = [dict(item) for item in list(first or []) if isinstance(item, dict)]
        second_records = [dict(item) for item in list(second or []) if isinstance(item, dict)]
        if len(first_records) != len(second_records):
            return False
        return all(
            cls._frontdoor_request_body_match_normalized_value(left)
            == cls._frontdoor_request_body_match_normalized_value(right)
            for left, right in zip(first_records, second_records)
        )

    @staticmethod
    def _frontdoor_actual_request_payload_from_path(request_path: str) -> dict[str, Any]:
        path = Path(str(request_path or "").strip())
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _frontdoor_actual_request_trace_candidate_paths(
        *,
        actual_request_path: str,
        actual_request_history: list[dict[str, Any]] | None,
    ) -> list[str]:
        candidates: list[str] = []
        for raw_path in [
            str(actual_request_path or "").strip(),
            *[
                str(item.get("path") or "").strip()
                for item in list(actual_request_history or [])
                if isinstance(item, dict)
            ],
        ]:
            if not raw_path or raw_path in candidates:
                continue
            candidates.append(raw_path)
        return candidates

    def _clear_frontdoor_actual_request_trace(self) -> None:
        self._frontdoor_actual_request_path = ""
        self._frontdoor_actual_request_history = []
        self._frontdoor_prompt_cache_key_hash = ""
        self._frontdoor_actual_request_hash = ""
        self._frontdoor_actual_request_message_count = 0
        self._frontdoor_actual_tool_schema_hash = ""

    def _apply_frontdoor_actual_request_trace_from_payload(
        self,
        payload: dict[str, Any] | None,
        *,
        artifact_path: str,
    ) -> bool:
        if not isinstance(payload, dict) or not payload:
            return False
        history_record = self._frontdoor_actual_request_history_record_from_payload(
            payload,
            artifact_path=str(artifact_path or "").strip(),
        )
        actual_request_path = str(artifact_path or "").strip()
        self._frontdoor_actual_request_path = actual_request_path
        self._frontdoor_actual_request_history = [history_record]
        self._frontdoor_prompt_cache_key_hash = str(
            payload.get("prompt_cache_key_hash") or history_record.get("prompt_cache_key_hash") or ""
        ).strip()
        self._frontdoor_actual_request_hash = str(
            payload.get("actual_request_hash") or history_record.get("actual_request_hash") or ""
        ).strip()
        self._frontdoor_actual_request_message_count = int(
            payload.get("actual_request_message_count")
            or history_record.get("actual_request_message_count")
            or 0
        )
        self._frontdoor_actual_tool_schema_hash = str(
            payload.get("actual_tool_schema_hash") or history_record.get("actual_tool_schema_hash") or ""
        ).strip()
        return True

    def _frontdoor_actual_request_trace_is_usable(self) -> bool:
        restored_baseline = [
            dict(item)
            for item in list(getattr(self, "_frontdoor_request_body_messages", []) or [])
            if isinstance(item, dict)
        ]
        if not restored_baseline:
            return False
        candidate_paths = self._frontdoor_actual_request_trace_candidate_paths(
            actual_request_path=str(getattr(self, "_frontdoor_actual_request_path", "") or "").strip(),
            actual_request_history=[
                dict(item)
                for item in list(getattr(self, "_frontdoor_actual_request_history", []) or [])
                if isinstance(item, dict)
            ],
        )
        for candidate_path in candidate_paths:
            payload = self._frontdoor_actual_request_payload_from_path(candidate_path)
            if not payload:
                continue
            request_messages = [
                dict(item)
                for item in list(payload.get("request_messages") or [])
                if isinstance(item, dict)
            ]
            durable_messages = self._restorable_frontdoor_request_body_messages(request_messages)
            if not self._frontdoor_request_body_messages_match(restored_baseline, durable_messages):
                continue
            return self._apply_frontdoor_actual_request_trace_from_payload(
                payload,
                artifact_path=candidate_path,
            )
        return False

    def _restore_frontdoor_actual_request_trace_from_matching_artifact(self, session_key: str) -> bool:
        try:
            from g3ku.runtime.web_ceo_sessions import actual_request_dir_for_session
        except Exception:
            return False
        restored_baseline = [
            dict(item)
            for item in list(getattr(self, "_frontdoor_request_body_messages", []) or [])
            if isinstance(item, dict)
        ]
        if not restored_baseline:
            return False
        request_dir = actual_request_dir_for_session(session_key, create=False)
        if not request_dir.exists():
            return False
        try:
            artifact_paths = sorted(request_dir.glob("*.json"), reverse=True)
        except Exception:
            return False
        for artifact_path in artifact_paths:
            payload = self._frontdoor_actual_request_payload_from_path(str(artifact_path.resolve()))
            if not payload:
                continue
            request_messages = [
                dict(item)
                for item in list(payload.get("request_messages") or [])
                if isinstance(item, dict)
            ]
            durable_messages = self._restorable_frontdoor_request_body_messages(request_messages)
            if not self._frontdoor_request_body_messages_match(restored_baseline, durable_messages):
                continue
            return self._apply_frontdoor_actual_request_trace_from_payload(
                payload,
                artifact_path=str(artifact_path.resolve()),
            )
        return False

    def _recompute_frontdoor_completed_continuity_bridge_pending(
        self,
        *,
        source: str,
        allow_continuity_bridge: bool,
    ) -> None:
        latest_history = [
            dict(item)
            for item in list(getattr(self, "_frontdoor_actual_request_history", []) or [])
            if isinstance(item, dict)
        ]
        bridge_path = str(getattr(self, "_frontdoor_actual_request_path", "") or "").strip()
        if not bridge_path and latest_history:
            bridge_path = str(latest_history[-1].get("path") or "").strip()
        self._frontdoor_completed_continuity_bridge_pending = bool(
            allow_continuity_bridge
            and str(source or "").strip() in {"paused_snapshot", "inflight_snapshot", "completed_continuity"}
            and bridge_path
            and Path(bridge_path).exists()
            and self._frontdoor_capability_snapshot_exposure_revision
            and self._frontdoor_provider_tool_schema_names
        )

    def _enrich_restored_frontdoor_actual_request_trace(
        self,
        *,
        session_key: str,
        source: str,
        allow_continuity_bridge: bool,
    ) -> None:
        if not self._frontdoor_actual_request_trace_is_usable():
            if not self._restore_frontdoor_actual_request_trace_from_matching_artifact(session_key):
                self._clear_frontdoor_actual_request_trace()
        self._recompute_frontdoor_completed_continuity_bridge_pending(
            source=source,
            allow_continuity_bridge=allow_continuity_bridge,
        )

    def _restore_frontdoor_state_from_payload(
        self,
        payload: dict[str, Any] | None,
        *,
        source: str,
        allow_continuity_bridge: bool,
    ) -> bool:
        if not isinstance(payload, dict) or not payload:
            return False
        frontdoor_request_body_messages = self._restorable_frontdoor_request_body_messages(
            payload.get("frontdoor_request_body_messages")
        )
        if not frontdoor_request_body_messages:
            return False
        self._frontdoor_request_body_messages = list(frontdoor_request_body_messages)
        self._frontdoor_history_shrink_reason = str(
            payload.get("frontdoor_history_shrink_reason") or ""
        ).strip()
        self._frontdoor_token_preflight_diagnostics = dict(
            payload.get("frontdoor_token_preflight_diagnostics") or {}
        )
        actual_request_path = str(
            payload.get("frontdoor_actual_request_path") or payload.get("actual_request_path") or ""
        ).strip()
        actual_request_history = [
            dict(item)
            for item in list(
                payload.get("frontdoor_actual_request_history") or payload.get("actual_request_history") or []
            )
            if isinstance(item, dict)
        ]
        self._frontdoor_actual_request_path = actual_request_path
        self._frontdoor_actual_request_history = actual_request_history
        self._frontdoor_stage_state = dict(payload.get("frontdoor_stage_state") or {})
        self._frontdoor_canonical_context = dict(payload.get("frontdoor_canonical_context") or {})
        self._compression_state = dict(
            payload.get("compression_state") or payload.get("compression") or {}
        )
        self._semantic_context_state = dict(payload.get("semantic_context_state") or {})
        self._frontdoor_hydrated_tool_names = self._normalized_name_list(
            payload.get("hydrated_tool_names")
        )
        self._frontdoor_capability_snapshot_exposure_revision = str(
            payload.get("capability_snapshot_exposure_revision") or ""
        ).strip()
        self._frontdoor_visible_tool_ids = self._normalized_name_list(payload.get("visible_tool_ids"))
        self._frontdoor_visible_skill_ids = self._normalized_name_list(payload.get("visible_skill_ids"))
        self._frontdoor_provider_tool_schema_names = self._normalized_name_list(
            payload.get("provider_tool_schema_names")
        )
        self._frontdoor_model_retry_status = None
        self._frontdoor_restore_source = str(source or "none").strip() or "none"
        self._frontdoor_baseline_sync_decision = str(
            payload.get("frontdoor_baseline_sync_decision") or ""
        ).strip()
        last_history = actual_request_history[-1] if actual_request_history else {}
        self._frontdoor_prompt_cache_key_hash = str(
            payload.get("frontdoor_prompt_cache_key_hash")
            or payload.get("prompt_cache_key_hash")
            or last_history.get("prompt_cache_key_hash")
            or ""
        ).strip()
        self._frontdoor_actual_request_hash = str(
            payload.get("frontdoor_actual_request_hash")
            or payload.get("actual_request_hash")
            or last_history.get("actual_request_hash")
            or ""
        ).strip()
        self._frontdoor_actual_request_message_count = int(
            payload.get("frontdoor_actual_request_message_count")
            or payload.get("actual_request_message_count")
            or last_history.get("actual_request_message_count")
            or 0
        )
        self._frontdoor_actual_tool_schema_hash = str(
            payload.get("frontdoor_actual_tool_schema_hash")
            or payload.get("actual_tool_schema_hash")
            or last_history.get("actual_tool_schema_hash")
            or ""
        ).strip()
        self._recompute_frontdoor_completed_continuity_bridge_pending(
            source=source,
            allow_continuity_bridge=allow_continuity_bridge,
        )
        return True

    def apply_history_truncation_state(
        self,
        payload: dict[str, Any] | None,
        *,
        removed_turn_ids: list[str] | None = None,
    ) -> None:
        """把编辑重发/Fork 截断后的连续性状态应用到 live 会话对象（就地变更）。

        调用方必须已持有 ``_turn_lock``。``payload`` 携带非空基线时走既有
        continuity 恢复路径；空基线（截断到会话开头）显式重置全部 frontdoor
        状态，让下一轮走无基线 cold 路径。无论哪个分支都清理轮级缓存，
        保证被截断轮次的内存状态不会泄漏进下一轮。
        """
        restored = False
        if isinstance(payload, dict) and payload:
            restored = self._restore_frontdoor_state_from_payload(
                payload,
                source="completed_continuity",
                allow_continuity_bridge=False,
            )
        if not restored:
            self._frontdoor_request_body_messages = []
            self._frontdoor_history_shrink_reason = str(
                (payload or {}).get("frontdoor_history_shrink_reason") or ""
            ).strip()
            self._frontdoor_pending_shrink_reason = ""
            self._frontdoor_token_preflight_diagnostics = {}
            self._frontdoor_actual_request_path = ""
            self._frontdoor_actual_request_history = []
            self._frontdoor_stage_state = {}
            self._frontdoor_canonical_context = default_frontdoor_canonical_context()
            self._compression_state = {}
            self._semantic_context_state = {}
            self._frontdoor_model_retry_status = None
            self._frontdoor_prompt_cache_key_hash = ""
            self._frontdoor_actual_request_hash = ""
            self._frontdoor_actual_request_message_count = 0
            self._frontdoor_actual_tool_schema_hash = ""
            self._frontdoor_restore_source = "none"
            self._frontdoor_baseline_sync_decision = ""
            self._frontdoor_token_compression_applied_turn = False
        self._frontdoor_previous_actual_request_path = ""
        self._frontdoor_previous_actual_request_history = []
        self._frontdoor_completed_continuity_bridge_pending = False
        self._frontdoor_selection_debug = {}
        self._frontdoor_repair_required_tool_items = []
        self._frontdoor_repair_required_skill_items = []
        # 轮级缓存清理：被截断轮次的任何内存状态都不得残留。
        removed = {
            str(item or "").strip()
            for item in list(removed_turn_ids or [])
            if str(item or "").strip()
        }
        for turn_id in removed:
            self._frontdoor_turn_usage.pop(turn_id, None)
        try:
            self._state.queued_follow_up_messages.clear()
        except Exception:
            pass
        self._preserved_inflight_turn = None
        self._follow_up_transition_snapshot = None
        self.clear_paused_execution_context()
        try:
            self._state.messages.clear()
        except Exception:
            pass
        self._active_turn_id = None
        self._clear_user_batch_context()
        self._last_verified_task_ids = []
        self._assistant_stream_pending_text = ""
        self._assistant_stream_last_emitted_text = ""
        self._assistant_segment_open = False

    def _restore_frontdoor_state_from_latest_actual_request_artifact(self, session_key: str) -> bool:
        try:
            from g3ku.runtime.web_ceo_sessions import actual_request_dir_for_session
        except Exception:
            return False
        request_dir = actual_request_dir_for_session(session_key, create=False)
        if not request_dir.exists():
            return False
        try:
            artifact_paths = sorted(request_dir.glob("*.json"), reverse=True)
        except Exception:
            return False
        for artifact_path in artifact_paths:
            try:
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            request_messages = [
                dict(item)
                for item in list(payload.get("request_messages") or [])
                if isinstance(item, dict)
            ]
            durable_messages = self._restorable_frontdoor_request_body_messages(request_messages)
            if not durable_messages:
                continue
            history_record = self._frontdoor_actual_request_history_record_from_payload(
                payload,
                artifact_path=str(artifact_path.resolve()),
            )
            return self._restore_frontdoor_state_from_payload(
                {
                    "frontdoor_request_body_messages": durable_messages,
                    "frontdoor_history_shrink_reason": str(
                        payload.get("frontdoor_history_shrink_reason") or ""
                    ).strip(),
                    "frontdoor_token_preflight_diagnostics": dict(
                        payload.get("frontdoor_token_preflight_diagnostics") or {}
                    ),
                    "frontdoor_actual_request_path": str(artifact_path.resolve()),
                    "frontdoor_actual_request_history": [history_record],
                    "prompt_cache_key_hash": str(payload.get("prompt_cache_key_hash") or "").strip(),
                    "actual_request_hash": str(payload.get("actual_request_hash") or "").strip(),
                    "actual_request_message_count": int(payload.get("actual_request_message_count") or 0),
                    "actual_tool_schema_hash": str(payload.get("actual_tool_schema_hash") or "").strip(),
                },
                source="actual_request_artifact",
                allow_continuity_bridge=False,
            )
        return False

    def _restore_frontdoor_persistent_state(self) -> str:
        session_key = str(self._state.session_key or "").strip()
        if not _frontdoor_continuity_session_key(session_key):
            self._frontdoor_restore_source = "none"
            return "none"
        if getattr(self._loop, "sessions", None) is None:
            self._frontdoor_restore_source = "none"
            return "none"
        try:
            from g3ku.runtime.web_ceo_sessions import (
                read_completed_continuity_snapshot,
                read_inflight_turn_snapshot,
                read_paused_execution_context,
            )
        except Exception:
            self._frontdoor_restore_source = "none"
            return "none"
        candidates = (
            ("paused_snapshot", read_paused_execution_context(session_key), True),
            ("inflight_snapshot", read_inflight_turn_snapshot(session_key), True),
            ("completed_continuity", read_completed_continuity_snapshot(session_key), True),
        )
        for source, payload, allow_bridge in candidates:
            if self._restore_frontdoor_state_from_payload(
                payload,
                source=source,
                allow_continuity_bridge=allow_bridge,
            ):
                self._enrich_restored_frontdoor_actual_request_trace(
                    session_key=session_key,
                    source=source,
                    allow_continuity_bridge=allow_bridge,
                )
                return self._frontdoor_restore_source
        if self._restore_frontdoor_state_from_latest_actual_request_artifact(session_key):
            return self._frontdoor_restore_source
        self._frontdoor_restore_source = "none"
        self._frontdoor_completed_continuity_bridge_pending = False
        return "none"

    def _consume_completed_continuity_bridge(
        self,
        *,
        current_visible_tool_ids: Any,
        current_visible_skill_ids: Any,
    ) -> dict[str, Any]:
        if not self._frontdoor_completed_continuity_bridge_pending:
            return {"pending": False, "enabled": False}
        self._frontdoor_completed_continuity_bridge_pending = False
        stored_tool_ids = self._normalized_name_list(
            self._frontdoor_visible_tool_ids,
            sort_values=True,
        )
        stored_skill_ids = self._normalized_name_list(
            self._frontdoor_visible_skill_ids,
            sort_values=True,
        )
        current_tool_ids = self._normalized_name_list(current_visible_tool_ids, sort_values=True)
        current_skill_ids = self._normalized_name_list(current_visible_skill_ids, sort_values=True)
        enabled = stored_tool_ids == current_tool_ids and stored_skill_ids == current_skill_ids
        return {
            "pending": True,
            "enabled": enabled,
            "exposure_revision": (
                self._frontdoor_capability_snapshot_exposure_revision if enabled else ""
            ),
            "provider_tool_schema_names": (
                list(self._frontdoor_provider_tool_schema_names) if enabled else []
            ),
        }

    def _sync_completed_continuity_snapshot(self, *, source_reason: str) -> None:
        session_key = str(self._state.session_key or "").strip()
        if not _frontdoor_continuity_session_key(session_key):
            return
        try:
            from g3ku.runtime.web_ceo_sessions import write_completed_continuity_snapshot
        except Exception:
            logger.debug("Completed continuity sync unavailable for {}", session_key)
            return
        payload: dict[str, Any] | None = None
        try:
            shrink_reason = str(getattr(self, "_frontdoor_history_shrink_reason", "") or "").strip()
            if not shrink_reason:
                shrink_reason = str(getattr(self, "_frontdoor_pending_shrink_reason", "") or "").strip()
            payload = {
                "frontdoor_request_body_messages": [
                    dict(item)
                    for item in list(getattr(self, "_frontdoor_request_body_messages", []) or [])
                    if isinstance(item, dict)
                ],
                "frontdoor_history_shrink_reason": shrink_reason,
                "frontdoor_token_preflight_diagnostics": copy.deepcopy(
                    getattr(self, "_frontdoor_token_preflight_diagnostics", None) or {}
                ),
                "frontdoor_actual_request_path": str(
                    getattr(self, "_frontdoor_actual_request_path", "") or ""
                ).strip(),
                "frontdoor_actual_request_history": [
                    dict(item)
                    for item in list(getattr(self, "_frontdoor_actual_request_history", []) or [])
                    if isinstance(item, dict)
                ],
                "frontdoor_stage_state": copy.deepcopy(
                    getattr(self, "_frontdoor_stage_state", None) or {}
                ),
                "frontdoor_canonical_context": copy.deepcopy(
                    getattr(self, "_frontdoor_canonical_context", None) or {}
                ),
                "compression_state": copy.deepcopy(getattr(self, "_compression_state", None) or {}),
                "semantic_context_state": copy.deepcopy(
                    getattr(self, "_semantic_context_state", None) or {}
                ),
                "hydrated_tool_names": list(
                    self._normalized_name_list(getattr(self, "_frontdoor_hydrated_tool_names", []))
                ),
                "capability_snapshot_exposure_revision": str(
                    getattr(self, "_frontdoor_capability_snapshot_exposure_revision", "") or ""
                ).strip(),
                "visible_tool_ids": list(
                    self._normalized_name_list(getattr(self, "_frontdoor_visible_tool_ids", []))
                ),
                "visible_skill_ids": list(
                    self._normalized_name_list(getattr(self, "_frontdoor_visible_skill_ids", []))
                ),
                "provider_tool_schema_names": list(
                    self._normalized_name_list(
                        getattr(self, "_frontdoor_provider_tool_schema_names", [])
                    )
                ),
                "frontdoor_restore_source": str(
                    getattr(self, "_frontdoor_restore_source", "none") or "none"
                ).strip()
                or "none",
                "frontdoor_baseline_sync_decision": str(
                    getattr(self, "_frontdoor_baseline_sync_decision", "") or ""
                ).strip(),
                "source_reason": str(source_reason or "").strip(),
            }
            write_completed_continuity_snapshot(session_key, payload)
        except Exception:
            logger.debug("Skipped completed continuity sync for {}", session_key)
            return
        # 每轮边界快照：与 completed continuity sidecar 同一份载荷按当前 turn_id upsert。
        # 同轮多次写互相覆盖，轮末 finalize 的写入即该轮终态；为编辑重发/Fork 提供
        # "截止该轮"的精确截断数据源。best-effort：失败只影响截断资格，不影响回合。
        try:
            from g3ku.runtime.web_ceo_sessions import write_turn_boundary_snapshot

            turn_id = str(getattr(self, "_active_turn_id", "") or "").strip()
            if turn_id and isinstance(payload, dict) and payload:
                write_turn_boundary_snapshot(session_key, turn_id, payload)
        except Exception:
            logger.debug("Skipped turn boundary snapshot for {}", session_key)

    def paused_execution_context_snapshot(self) -> dict[str, Any] | None:
        if self._paused_execution_context is not None:
            return copy.deepcopy(self._paused_execution_context)
        session_key = str(self._state.session_key or "").strip()
        if not session_key.startswith("web:"):
            return None
        try:
            from g3ku.runtime.web_ceo_sessions import read_paused_execution_context

            snapshot = read_paused_execution_context(session_key)
        except Exception:
            logger.debug("paused execution context restore skipped for {}", session_key)
            return None
        if isinstance(snapshot, dict) and snapshot:
            self._paused_execution_context = copy.deepcopy(snapshot)
            return copy.deepcopy(self._paused_execution_context)
        return None

    def _set_paused_execution_context(self, snapshot: dict[str, Any] | None) -> None:
        self._paused_execution_context = copy.deepcopy(snapshot) if isinstance(snapshot, dict) and snapshot else None
        self._sync_persisted_paused_execution_context()

    def clear_paused_execution_context(self) -> None:
        self._paused_execution_context = None
        self._sync_persisted_paused_execution_context()

    def _preserve_frontdoor_actual_request_trace_for_next_visible_turn(self) -> None:
        actual_request_path = str(getattr(self, "_frontdoor_actual_request_path", "") or "").strip()
        actual_request_history = [
            dict(item)
            for item in list(getattr(self, "_frontdoor_actual_request_history", []) or [])
            if isinstance(item, dict)
        ]
        if actual_request_path or actual_request_history:
            self._frontdoor_previous_actual_request_path = actual_request_path
            self._frontdoor_previous_actual_request_history = actual_request_history

    def _normalize_live_context(self, live_context: dict[str, str] | None) -> dict[str, str]:
        current_channel = str(getattr(self, "_channel", "") or "cli").strip() or "cli"
        current_chat_id = str(getattr(self, "_chat_id", "") or "direct").strip() or "direct"
        current_memory_channel = (
            str(getattr(self, "_memory_channel", "") or current_channel).strip() or current_channel
        )
        current_memory_chat_id = (
            str(getattr(self, "_memory_chat_id", "") or current_chat_id).strip() or current_chat_id
        )
        payload = live_context if isinstance(live_context, dict) else {}
        return {
            "channel": str(payload.get("channel") or current_channel).strip() or current_channel,
            "chat_id": str(payload.get("chat_id") or current_chat_id).strip() or current_chat_id,
            "memory_channel": str(payload.get("memory_channel") or current_memory_channel).strip()
            or current_memory_channel,
            "memory_chat_id": str(payload.get("memory_chat_id") or current_memory_chat_id).strip()
            or current_memory_chat_id,
        }

    def _apply_live_context(self, live_context: dict[str, str] | None) -> None:
        normalized = self._normalize_live_context(live_context)
        self._channel = normalized["channel"]
        self._chat_id = normalized["chat_id"]
        self._memory_channel = normalized["memory_channel"]
        self._memory_chat_id = normalized["memory_chat_id"]

    def _now(self) -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _history_text(content: Any) -> str:
        if isinstance(content, str):
            return content
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
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(content or "")

    @staticmethod
    def _turn_metadata_value(message: dict[str, Any], key: str) -> str:
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        return str(metadata.get(key) or "").strip()

    @classmethod
    def _message_turn_id(cls, message: dict[str, Any]) -> str:
        return cls._turn_metadata_value(message, _TRANSCRIPT_TURN_ID_KEY)

    @classmethod
    def _message_transcript_state(cls, message: dict[str, Any]) -> str:
        return cls._turn_metadata_value(message, _TRANSCRIPT_STATE_KEY)

    @classmethod
    def _message_batch_id(cls, message: dict[str, Any]) -> str:
        return cls._turn_metadata_value(message, _TRANSCRIPT_BATCH_ID_KEY)

    @staticmethod
    def _build_turn_metadata(
        metadata: dict[str, Any] | None,
        *,
        turn_id: str,
        transcript_state: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(metadata or {})
        payload[_TRANSCRIPT_TURN_ID_KEY] = str(turn_id or "").strip()
        payload[_TRANSCRIPT_STATE_KEY] = str(transcript_state or "").strip()
        resolved_batch_id = str(batch_id or payload.get(_TRANSCRIPT_BATCH_ID_KEY) or "").strip()
        if resolved_batch_id:
            payload[_TRANSCRIPT_BATCH_ID_KEY] = resolved_batch_id
        return payload

    @staticmethod
    def _internal_prompt_message_metadata(
        *,
        source: str,
        internal_prompt_kind: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "source": str(source or "").strip().lower(),
            "prompt_visible": True,
            "ui_visible": False,
            "internal_prompt_kind": str(internal_prompt_kind or "").strip(),
        }
        for key, value in dict(extra or {}).items():
            if value in (None, "", [], {}):
                continue
            payload[str(key)] = value
        return payload

    @staticmethod
    def _new_turn_id() -> str:
        return uuid.uuid4().hex[:16]

    @staticmethod
    def _new_batch_id() -> str:
        return uuid.uuid4().hex[:16]

    @staticmethod
    def _serialize_pending_interrupts(values: list[Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in list(values or []):
            items.append(
                {
                    "id": str(getattr(raw, "interrupt_id", getattr(raw, "id", "")) or ""),
                    "value": getattr(raw, "value", None),
                }
            )
        return items

    @staticmethod
    def _normalize_verified_task_ids(values: Any) -> list[str]:
        items = list(values) if isinstance(values, (list, tuple, set)) else [values]
        normalized: list[str] = []
        for raw in items:
            task_id = str(raw or "").strip()
            if not task_id.startswith("task:") or task_id in normalized:
                continue
            normalized.append(task_id)
        return normalized

    @classmethod
    def _extract_task_ids_from_text(cls, value: Any) -> list[str]:
        return cls._normalize_verified_task_ids(_TASK_ID_PATTERN.findall(str(value or "")))

    def _successful_async_dispatch_task_ids(self, interaction_flow: list[dict[str, Any]]) -> list[str]:
        task_ids: list[str] = []
        for item in reversed(list(interaction_flow or [])):
            if str(item.get("tool_name") or "").strip() != "create_async_task":
                continue
            if str(item.get("status") or "").strip().lower() != "success":
                continue
            for candidate in (
                item.get("text"),
                item.get("output_text"),
                item.get("output_preview_text"),
                item.get("arguments_text"),
            ):
                for task_id in self._extract_task_ids_from_text(candidate):
                    if task_id not in task_ids:
                        task_ids.append(task_id)
        return task_ids

    @classmethod
    def _task_ids_from_canonical_context(cls, canonical_context: dict[str, Any] | None) -> list[str]:
        task_ids: list[str] = []
        if not isinstance(canonical_context, dict):
            return task_ids
        for tool in canonical_context_tool_items(canonical_context):
            if not isinstance(tool, dict):
                continue
            for candidate in (
                tool.get("output_text"),
                tool.get("output_preview_text"),
                tool.get("arguments_text"),
            ):
                for task_id in cls._extract_task_ids_from_text(candidate):
                    if task_id not in task_ids:
                        task_ids.append(task_id)
        return task_ids

    @classmethod
    def _complete_active_frontdoor_stage_state(
        cls,
        stage_state: dict[str, Any] | None,
        *,
        completed_stage_summary: str = "",
    ) -> dict[str, Any]:
        normalized_state = dict(stage_state or {})
        active_stage_id = str(normalized_state.get("active_stage_id") or "").strip()
        if not active_stage_id:
            return normalized_state
        now = datetime.now().isoformat()
        normalized_summary = str(completed_stage_summary or "").strip()
        stages: list[dict[str, Any]] = []
        completed_any = False
        for raw_stage in list(normalized_state.get("stages") or []):
            current = dict(raw_stage) if isinstance(raw_stage, dict) else {}
            if (
                str(current.get("stage_id") or "").strip() == active_stage_id
                and str(current.get("status") or "").strip().lower() == "active"
            ):
                current["status"] = "completed"
                current["finished_at"] = str(current.get("finished_at") or "").strip() or now
                if normalized_summary and not str(current.get("completed_stage_summary") or "").strip():
                    current["completed_stage_summary"] = normalized_summary
                completed_any = True
            stages.append(current)
        return {
            "active_stage_id": "" if completed_any else active_stage_id,
            "transition_required": False if completed_any else bool(normalized_state.get("transition_required")),
            "stages": stages,
            "pending_orphan_rounds": [
                dict(item)
                for item in list(normalized_state.get("pending_orphan_rounds") or [])
                if isinstance(item, dict)
            ],
        }

    def _recover_dispatched_async_runtime_error(
        self,
        exc: Exception,
        *,
        interaction_flow: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        from g3ku.providers.fallback import is_internal_runtime_model_error

        if not is_internal_runtime_model_error(exc):
            return None
        task_ids = self._successful_async_dispatch_task_ids(interaction_flow)
        if not task_ids:
            return None
        primary_task_id = task_ids[0]
        fallback_text = (
            f"后台任务已经建立，任务号 `{primary_task_id}`。"
            "当前回写遇到暂时异常，但后台任务仍在运行，完成后会继续同步结果。"
        )
        recovered_visible_text = self._best_effort_async_dispatch_visible_text(task_ids=task_ids)
        if recovered_visible_text:
            if any(task_id in recovered_visible_text for task_id in task_ids):
                recovered_text = recovered_visible_text
            else:
                recovered_text = f"{recovered_visible_text}\n\n{fallback_text}"
        else:
            recovered_text = fallback_text
        return {
            "text": recovered_text,
            "task_ids": task_ids,
        }

    @staticmethod
    def _frontdoor_stage_summary_candidates(stage_state: dict[str, Any] | None) -> list[str]:
        if not isinstance(stage_state, dict):
            return []
        summaries: list[str] = []
        for raw_stage in reversed(list(stage_state.get("stages") or [])):
            if not isinstance(raw_stage, dict):
                continue
            summary = str(raw_stage.get("completed_stage_summary") or "").strip()
            if not summary or summary == STAGE_TURN_END_SUMMARY_POINTER:
                continue
            if summary not in summaries:
                summaries.append(summary)
        return summaries

    def _best_effort_async_dispatch_visible_text(self, *, task_ids: list[str]) -> str:
        candidates: list[str] = []
        latest_message = str(self._state.latest_message or "").strip()
        if latest_message:
            candidates.append(latest_message)
        for summary in self._frontdoor_stage_summary_candidates(getattr(self, "_frontdoor_stage_state", None)):
            if summary not in candidates:
                candidates.append(summary)
        if not candidates:
            return ""
        normalized_task_ids = self._normalize_verified_task_ids(task_ids)
        if normalized_task_ids:
            for candidate in candidates:
                if any(task_id in candidate for task_id in normalized_task_ids):
                    return candidate
        return candidates[0]

    def _ensure_user_turn_id(self, user_input: UserInputMessage, *, reuse_active: bool = True) -> str:
        metadata = dict(user_input.metadata or {})
        active_turn_id = self._active_turn_id if reuse_active else None
        turn_id = str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or active_turn_id or "").strip()
        if not turn_id:
            turn_id = self._new_turn_id()
        if metadata.get(_TRANSCRIPT_TURN_ID_KEY) != turn_id:
            metadata[_TRANSCRIPT_TURN_ID_KEY] = turn_id
            user_input.metadata = metadata
        self._active_turn_id = turn_id
        return turn_id

    def _ensure_user_batch_id(self, user_input: UserInputMessage, *, batch_id: str | None = None) -> str:
        metadata = dict(user_input.metadata or {})
        resolved_batch_id = str(
            batch_id
            or metadata.get(_TRANSCRIPT_BATCH_ID_KEY)
            or self._active_batch_id
            or ""
        ).strip()
        if not resolved_batch_id:
            resolved_batch_id = self._new_batch_id()
        if metadata.get(_TRANSCRIPT_BATCH_ID_KEY) != resolved_batch_id:
            metadata[_TRANSCRIPT_BATCH_ID_KEY] = resolved_batch_id
            user_input.metadata = metadata
        self._active_batch_id = resolved_batch_id
        return resolved_batch_id

    def _configure_user_batch(
        self,
        user_inputs: list[UserInputMessage],
        *,
        batch_id: str | None = None,
    ) -> list[UserInputMessage]:
        configured: list[UserInputMessage] = []
        resolved_batch_id = str(batch_id or "").strip() or None
        for item in list(user_inputs or []):
            if not isinstance(item, UserInputMessage):
                continue
            self._ensure_user_turn_id(item, reuse_active=False)
            resolved_batch_id = self._ensure_user_batch_id(item, batch_id=resolved_batch_id)
            configured.append(item)
        self._active_user_batch_inputs = list(configured)
        self._active_batch_id = str(resolved_batch_id or "").strip() or None
        return configured

    def _current_user_batch_inputs(self, fallback_user_input: UserInputMessage | None = None) -> list[UserInputMessage]:
        if self._active_user_batch_inputs:
            return list(self._active_user_batch_inputs)
        return [fallback_user_input] if isinstance(fallback_user_input, UserInputMessage) else []

    def _clear_user_batch_context(self) -> None:
        self._active_user_batch_inputs = []
        self._active_batch_id = None

    def _batch_query_text(self, user_inputs: list[UserInputMessage]) -> str:
        parts: list[str] = []
        for user_input in list(user_inputs or []):
            text = self._history_text(getattr(user_input, "content", ""))
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts).strip()

    def _current_turn_id(self, prompt: Any | None = None) -> str:
        current = self._last_prompt if prompt is None else prompt
        if isinstance(current, UserInputMessage):
            if self._internal_prompt_source(current) is None:
                return self._ensure_user_turn_id(current)
            metadata = dict(current.metadata or {})
            turn_id = str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or self._active_turn_id or "").strip()
            if not turn_id:
                turn_id = self._new_turn_id()
                metadata[_TRANSCRIPT_TURN_ID_KEY] = turn_id
                current.metadata = metadata
                self._active_turn_id = turn_id
            return turn_id
        return str(self._active_turn_id or "").strip()

    @classmethod
    def _find_transcript_user_index(cls, persisted_session: Any, *, turn_id: str) -> int | None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return None
        messages = list(getattr(persisted_session, "messages", []) or [])
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").strip().lower() != "user":
                continue
            if cls._message_turn_id(message) != normalized_turn_id:
                continue
            return index
        return None

    @staticmethod
    def _message_top_level_turn_id(message: dict[str, Any]) -> str:
        return str(message.get("turn_id") or "").strip()

    @classmethod
    def _find_archived_paused_assistant_index(cls, persisted_session: Any, *, turn_id: str) -> int | None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return None
        messages = list(getattr(persisted_session, "messages", []) or [])
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").strip().lower() != "assistant":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if str(metadata.get("source") or "").strip().lower() != "manual_pause_archive":
                continue
            if cls._message_top_level_turn_id(message) != normalized_turn_id:
                continue
            return index
        return None

    def _upsert_transcript_user_message(
        self,
        *,
        persisted_session: Any,
        user_input: UserInputMessage,
        user_text: str,
        transcript_state: str,
    ) -> None:
        turn_id = self._ensure_user_turn_id(user_input)
        batch_id = self._ensure_user_batch_id(user_input)
        metadata = self._build_turn_metadata(
            dict(user_input.metadata or {}),
            turn_id=turn_id,
            transcript_state=transcript_state,
            batch_id=batch_id,
        )
        user_input.metadata = metadata
        existing_index = self._find_transcript_user_index(persisted_session, turn_id=turn_id)
        if existing_index is None:
            persisted_session.add_message(
                "user",
                user_text,
                attachments=list(user_input.attachments or []),
                metadata=metadata,
            )
            return
        existing = dict(persisted_session.messages[existing_index])
        existing["content"] = user_text
        existing["attachments"] = list(user_input.attachments or [])
        existing["metadata"] = metadata
        if not str(existing.get("timestamp") or "").strip():
            existing["timestamp"] = self._now()
        persisted_session.messages[existing_index] = existing
        if hasattr(persisted_session, "updated_at"):
            persisted_session.updated_at = datetime.now()

    def _complete_lingering_paused_user_messages(self, persisted_session: Any) -> int:
        """一个用户可见回合正常完成后，把仍残留 paused 状态的转录用户条目提升为 completed。

        paused 条目由 `_persist_manual_pause_user_messages` 写入：手动暂停的回合不会
        再走完成路径，也没有其他路径把它们推进到 completed。而
        `_reconcile_paused_user_turns_into_seed`（frontdoor 续跑种子对账）会在每个回合
        开始时把所有 prompt-visible 的 paused 用户消息补到种子尾部——于是卡在 paused
        的历史消息会在之后每一轮的请求体尾部、紧邻当前用户消息且没有任何助手回复地
        反复出现，制造“从未被回答的用户提问”的假象，诱导模型重复处理早已回答过的
        问题（详见 docs/architecture/context-and-cache-troubleshooting.md 的“残留
        paused 转录条目”陷阱）。当一个新的用户回合正常完成时，这些残留条目要么已经
        被对账进本轮或前几轮的种子并得到处理，要么已被新输入取代，应随回合完成退役。

        注意：运行时错误路径不能调用本方法——出错回合的请求体未必完成基线回写，
        此时把 paused 条目提升为 completed 会让对账机制停止补发，用户消息可能从此
        在模型上下文里消失。"""
        flipped = 0
        messages = getattr(persisted_session, "messages", None)
        if not isinstance(messages, list):
            return 0
        for index, raw in enumerate(list(messages)):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("role") or "").strip().lower() != "user":
                continue
            metadata = raw.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get(_TRANSCRIPT_STATE_KEY) or "").strip().lower() != _TRANSCRIPT_STATE_PAUSED:
                continue
            updated = dict(raw)
            updated_metadata = dict(metadata)
            updated_metadata[_TRANSCRIPT_STATE_KEY] = _TRANSCRIPT_STATE_COMPLETED
            updated["metadata"] = updated_metadata
            messages[index] = updated
            flipped += 1
        if flipped:
            logger.debug(
                "Completed {} lingering paused transcript user message(s) for {}",
                flipped,
                self._state.session_key,
            )
        return flipped

    def _retire_consumed_pending_user_messages(self, persisted_session: Any) -> int:
        """回合正常完成后，把转录里已不再是排队真相的 pending 用户行翻成 completed。

        pending 行是排队队列的 durable 记录：`queue_follow_up_batch` 入队时写它，
        `_rehydrate_queued_follow_ups` 在会话重建时按它接回队列。回合完成时内存队列
        里已经没有这一条，说明本进程不再打算派发它（已被某轮消费，或被更新的输入取
        代）；行留在 pending 只会让下一次重建把它当作从未回答的提问重新投喂。

        主要漏点是内部（心跳/cron）回合：它在 prepare 阶段清空批次上下文，完成时又
        整段跳过用户行回写，于是它中途消费的 follow-up 行永远停在 pending。

        与 `_complete_lingering_paused_user_messages` 同一条硬约束：只在正常完成路径
        调用。错误/取消路径的内存队列可能仍持有这些条目，提前翻体会让 durable 那一
        半失去真相，消息从此在模型上下文里消失。"""
        messages = getattr(persisted_session, "messages", None)
        if not isinstance(messages, list):
            return 0
        live_turn_ids = {
            self._user_input_turn_id(item)
            for item in list(self._state.queued_follow_up_messages or [])
            if self._user_input_turn_id(item)
        }
        flipped = 0
        for index, raw in enumerate(list(messages)):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("role") or "").strip().lower() != "user":
                continue
            metadata = raw.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get(_TRANSCRIPT_STATE_KEY) or "").strip().lower() != _TRANSCRIPT_STATE_PENDING:
                continue
            turn_id = str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or "").strip()
            if turn_id and turn_id in live_turn_ids:
                continue
            updated = dict(raw)
            updated_metadata = dict(metadata)
            updated_metadata[_TRANSCRIPT_STATE_KEY] = _TRANSCRIPT_STATE_COMPLETED
            updated["metadata"] = updated_metadata
            messages[index] = updated
            flipped += 1
        if flipped:
            logger.info(
                "Retired {} consumed pending transcript user message(s) for {}",
                flipped,
                self._state.session_key,
            )
        return flipped

    def _discard_internal_prompt_messages(self, persisted_session: Any, turn_id: str) -> int:
        """把指定 turn 的内部提示词消息（心跳/cron 规则 system + 事件束 user）翻成 discarded。

        这些消息在模型调用前就由 `_persist_internal_prompt_messages` 以 completed 落盘；
        回合失败时若不回收，会在之后每一轮请求体里反复堆积。翻成 discarded 后
        `is_prompt_visible_message` 会将其排除出可重放上下文（jsonl 原始行保留以备审计）。

        只匹配带 `internal_prompt_kind` 的内部提示词行，避免误伤同 turn 的助手错误行。
        """
        resolved_turn_id = str(turn_id or "").strip()
        if not resolved_turn_id:
            return 0
        messages = getattr(persisted_session, "messages", None)
        if not isinstance(messages, list):
            return 0
        flipped = 0
        for index, raw in enumerate(list(messages)):
            if not isinstance(raw, dict):
                continue
            metadata = raw.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or "").strip() != resolved_turn_id:
                continue
            if str(metadata.get("internal_prompt_kind") or "").strip() not in _INTERNAL_PROMPT_KINDS:
                continue
            if str(metadata.get(_TRANSCRIPT_STATE_KEY) or "").strip().lower() == _TRANSCRIPT_STATE_DISCARDED:
                continue
            updated = dict(raw)
            updated_metadata = dict(metadata)
            updated_metadata[_TRANSCRIPT_STATE_KEY] = _TRANSCRIPT_STATE_DISCARDED
            updated["metadata"] = updated_metadata
            messages[index] = updated
            flipped += 1
        if flipped:
            if hasattr(persisted_session, "updated_at"):
                persisted_session.updated_at = datetime.now()
            logger.debug(
                "Discarded {} internal prompt transcript message(s) for failed turn {} ({})",
                flipped,
                resolved_turn_id,
                self._state.session_key,
            )
        return flipped

    async def _archive_inflight_assistant_for_follow_up_ui_history(
        self,
        *,
        snapshot_override: dict[str, Any] | None = None,
        pending_follow_up_turn_ids: set[str] | None = None,
    ) -> Any | None:
        snapshot = copy.deepcopy(snapshot_override) if isinstance(snapshot_override, dict) else self._current_inflight_turn_snapshot()
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        source = str(snapshot.get("source") or "").strip().lower()
        if source in {"heartbeat", "cron", "approval"}:
            return None
        canonical_context = (
            copy.deepcopy(snapshot.get("canonical_context"))
            if isinstance(snapshot.get("canonical_context"), dict)
            else {}
        )
        compression = (
            copy.deepcopy(snapshot.get("compression"))
            if isinstance(snapshot.get("compression"), dict)
            else {}
        )
        assistant_text = str(snapshot.get("assistant_text") or "").strip()
        if not assistant_text and not canonical_context and not compression:
            return None
        archived_text = assistant_text or "处理中..."
        archived_from_turn_id = str(snapshot.get("turn_id") or "").strip()
        metadata = {
            "source": "follow_up_archive",
            "prompt_visible": False,
            "ui_visible": True,
            "history_visible": True,
            "archived_follow_up_turn": True,
        }
        if archived_from_turn_id:
            metadata["archived_from_turn_id"] = archived_from_turn_id
        archived_task_ids = self._task_ids_from_canonical_context(canonical_context)
        if archived_task_ids:
            metadata["task_ids"] = archived_task_ids
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            last_message = (
                persisted_session.messages[-1]
                if getattr(persisted_session, "messages", None)
                else None
            )
            if isinstance(last_message, dict):
                last_metadata = dict(last_message.get("metadata") or {})
                if (
                    str(last_message.get("role") or "").strip().lower() == "assistant"
                    and str(last_metadata.get("source") or "").strip().lower() == "follow_up_archive"
                    and str(last_metadata.get("archived_from_turn_id") or "").strip() == archived_from_turn_id
                    and str(last_message.get("content") or "") == archived_text
                ):
                    return persisted_session
            archive_turn_id = (
                f"{archived_from_turn_id}:followup:{self._new_turn_id()}"
                if archived_from_turn_id
                else f"followup:{self._new_turn_id()}"
            )
            assistant_payload: dict[str, Any] = {
                "turn_id": archive_turn_id,
                "metadata": metadata,
            }
            if canonical_context:
                projected = _project_transcript_canonical_context(canonical_context)
                if projected:
                    assistant_payload.update(
                        plan_transcript_cc_row(getattr(persisted_session, "messages", None), projected)
                    )
            if compression:
                assistant_payload["compression"] = compression
            persisted_session.add_message("assistant", archived_text, **assistant_payload)
            if isinstance(getattr(persisted_session, "messages", None), list) and persisted_session.messages:
                archived_message = persisted_session.messages.pop()
                insert_index = len(persisted_session.messages)
                target_turn_ids = {
                    str(item or "").strip()
                    for item in list(pending_follow_up_turn_ids or set())
                    if str(item or "").strip()
                }
                if target_turn_ids:
                    for index, raw in enumerate(list(persisted_session.messages or [])):
                        if not isinstance(raw, dict):
                            continue
                        if str(raw.get("role") or "").strip().lower() != "user":
                            continue
                        if self._message_turn_id(raw) not in target_turn_ids:
                            continue
                        insert_index = index
                        break
                persisted_session.messages.insert(insert_index, archived_message)
            self._loop.sessions.save(persisted_session)
            return persisted_session
        except Exception:
            logger.debug("Skipped follow-up archive persistence for {}", self._state.session_key)
        return None

    def _capture_follow_up_transition_snapshot(self) -> None:
        snapshot = self._current_inflight_turn_snapshot()
        if not isinstance(snapshot, dict) or not snapshot:
            return
        source = str(snapshot.get("source") or "").strip().lower()
        status = str(snapshot.get("status") or "").strip().lower()
        if source in {"heartbeat", "cron", "approval"}:
            return
        if status not in {"running", "in_progress", "active"}:
            return
        if (
            not str(snapshot.get("assistant_text") or "").strip()
            and not isinstance(snapshot.get("canonical_context"), dict)
            and not isinstance(snapshot.get("compression"), dict)
        ):
            return
        self._follow_up_transition_snapshot = copy.deepcopy(snapshot)

    async def archive_follow_up_chain_transition(
        self,
        *,
        pending_follow_up_turn_ids: set[str] | None = None,
    ) -> Any | None:
        snapshot = copy.deepcopy(self._follow_up_transition_snapshot) if isinstance(self._follow_up_transition_snapshot, dict) else None
        self._follow_up_transition_snapshot = None
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        return await self._archive_inflight_assistant_for_follow_up_ui_history(
            snapshot_override=snapshot,
            pending_follow_up_turn_ids=pending_follow_up_turn_ids,
        )

    async def _archive_paused_execution_context_for_ui_history(self) -> None:
        snapshot = self.paused_execution_context_snapshot()
        if not isinstance(snapshot, dict) or not snapshot:
            return
        if str(snapshot.get("status") or "").strip().lower() != "paused":
            return
        source = str(snapshot.get("source") or "").strip().lower()
        if source in {"heartbeat", "cron", "approval"}:
            return
        user_message = snapshot.get("user_message") if isinstance(snapshot.get("user_message"), dict) else None
        if not isinstance(user_message, dict):
            return
        if not str(user_message.get("content") or "").strip() and not list(user_message.get("attachments") or []):
            return
        turn_id = str(snapshot.get("turn_id") or "").strip()
        if not turn_id:
            return
        assistant_text = str(snapshot.get("assistant_text") or "").strip() or "已暂停"
        canonical_context = (
            copy.deepcopy(snapshot.get("canonical_context"))
            if isinstance(snapshot.get("canonical_context"), dict)
            else {}
        )
        compression = (
            copy.deepcopy(snapshot.get("compression"))
            if isinstance(snapshot.get("compression"), dict)
            else {}
        )
        metadata = {
            "history_visible": False,
            "source": "manual_pause_archive",
            "archived_paused_turn": True,
        }
        archived_task_ids = self._task_ids_from_canonical_context(canonical_context)
        if archived_task_ids:
            metadata["task_ids"] = archived_task_ids
        assistant_payload: dict[str, Any] = {
            "turn_id": turn_id,
            "status": "paused",
            "metadata": metadata,
        }
        projected_paused_context: dict[str, Any] = {}
        if canonical_context:
            projected_paused_context = _project_transcript_canonical_context(canonical_context)
        if compression:
            assistant_payload["compression"] = compression
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            existing_index = self._find_archived_paused_assistant_index(persisted_session, turn_id=turn_id)
            if existing_index is None:
                if projected_paused_context:
                    # 暂停归档行永远落全量 checkpoint：它可能被后续替换重写，
                    # delta 形态会让那次就地变更牵连下游链。
                    assistant_payload.update(
                        {
                            "canonical_context": projected_paused_context,
                            "canonical_context_projection": TRANSCRIPT_PROJECTION_MODE,
                        }
                    )
                persisted_session.add_message("assistant", assistant_text, **assistant_payload)
            else:
                # 修复下游链要先按旧链取回被替换行的原视图。
                replaced_old_view = materialize_transcript_view(
                    persisted_session.messages,
                    existing_index,
                )
                archived_message = dict(persisted_session.messages[existing_index])
                archived_message["content"] = assistant_text
                archived_message["turn_id"] = turn_id
                archived_message["status"] = "paused"
                archived_message["metadata"] = metadata
                if projected_paused_context:
                    archived_message["canonical_context"] = projected_paused_context
                    archived_message["canonical_context_projection"] = TRANSCRIPT_PROJECTION_MODE
                else:
                    archived_message.pop("canonical_context", None)
                    archived_message.pop("canonical_context_projection", None)
                if compression:
                    archived_message["compression"] = compression
                else:
                    archived_message.pop("compression", None)
                persisted_session.messages[existing_index] = archived_message
                # 就地替换了轨道行：以旧视图为锚的后续 delta 行按新链重编码。
                repair_transcript_cc_chain(
                    persisted_session.messages,
                    existing_index,
                    replaced_old_view,
                )
                if hasattr(persisted_session, "updated_at"):
                    persisted_session.updated_at = datetime.now()
            if self._state.session_key.startswith("web:"):
                from g3ku.runtime.web_ceo_sessions import ensure_ceo_session_metadata

                ensure_ceo_session_metadata(persisted_session)
            self._loop.sessions.save(persisted_session)
        except Exception:
            await self._emit(
                "message_delta",
                channel="analysis",
                kind="persistence_warning",
                text="Paused assistant history archival failed; the paused bubble is still available in snapshot state.",
            )

    async def _persist_pending_user_message(self, *, user_input: UserInputMessage, user_text: str) -> Any | None:
        return await self._persist_pending_user_messages(user_inputs=[user_input])

    async def _persist_pending_user_messages(self, *, user_inputs: list[UserInputMessage]) -> Any | None:
        normalized_inputs = [
            item
            for item in list(user_inputs or [])
            if isinstance(item, UserInputMessage)
        ]
        if not normalized_inputs:
            return None
        visible_texts = [
            self._history_text(item.content)
            for item in normalized_inputs
            if self._history_text(item.content).strip() or item.attachments
        ]
        if not visible_texts and not any(item.attachments for item in normalized_inputs):
            return None
        persisted_session = None
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            for item in normalized_inputs:
                user_text = self._history_text(item.content)
                if not user_text.strip() and not item.attachments:
                    continue
                self._upsert_transcript_user_message(
                    persisted_session=persisted_session,
                    user_input=item,
                    user_text=user_text,
                    transcript_state=_TRANSCRIPT_STATE_PENDING,
                )
            if self._state.session_key.startswith("web:"):
                from g3ku.runtime.web_ceo_sessions import update_ceo_session_after_turn

                update_ceo_session_after_turn(
                    persisted_session,
                    user_text=visible_texts[-1] if visible_texts else "",
                    assistant_text="",
                    route_kind="",
                )
            self._loop.sessions.save(persisted_session)
        except Exception:
            logger.debug("Pending transcript persistence skipped for {}", self._state.session_key)
        return persisted_session

    async def _persist_internal_prompt_messages(
        self,
        *,
        user_input: UserInputMessage,
        internal_source: str,
    ) -> Any | None:
        normalized_source = str(internal_source or "").strip().lower()
        if normalized_source not in {"heartbeat", "cron"}:
            return None
        persisted_session = None
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            turn_id = self._ensure_user_turn_id(user_input)
            base_metadata = self._build_turn_metadata(
                {},
                turn_id=turn_id,
                transcript_state=_TRANSCRIPT_STATE_COMPLETED,
            )
            prompt_metadata = dict(user_input.metadata or {})
            if normalized_source == "heartbeat":
                rule_text = str(prompt_metadata.get("heartbeat_stable_rules_text") or "").strip()
                event_text = str(
                    prompt_metadata.get("heartbeat_event_bundle_text")
                    or self._history_text(user_input.content)
                    or ""
                ).strip()
                if rule_text:
                    persisted_session.add_message(
                        "system",
                        rule_text,
                        metadata={
                            **base_metadata,
                            **self._internal_prompt_message_metadata(
                                source=normalized_source,
                                internal_prompt_kind="heartbeat_rule",
                            ),
                        },
                    )
                if event_text:
                    persisted_session.add_message(
                        "user",
                        event_text,
                        metadata={
                            **base_metadata,
                            **self._internal_prompt_message_metadata(
                                source=normalized_source,
                                internal_prompt_kind="heartbeat_event_bundle",
                                extra={"heartbeat_internal": True},
                            ),
                        },
                    )
            elif normalized_source == "cron":
                cron_job_id = str(prompt_metadata.get("cron_job_id") or "").strip()
                from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

                cron_system_message = CeoFrontDoorSupport._cron_internal_system_message(prompt_metadata)
                cron_event_message = CeoFrontDoorSupport._cron_internal_event_message(
                    prompt_metadata,
                    reminder_text=str(self._history_text(user_input.content) or "").strip(),
                )
                rule_text = (
                    str(cron_system_message.get("content") or "").strip()
                    if isinstance(cron_system_message, dict)
                    else ""
                )
                event_text = (
                    str(cron_event_message.get("content") or "").strip()
                    if isinstance(cron_event_message, dict)
                    else ""
                )
                if rule_text:
                    persisted_session.add_message(
                        "system",
                        rule_text,
                        metadata={
                            **base_metadata,
                            **self._internal_prompt_message_metadata(
                                source=normalized_source,
                                internal_prompt_kind="cron_rule",
                                extra={"cron_job_id": cron_job_id},
                            ),
                        },
                    )
                if event_text:
                    persisted_session.add_message(
                        "system",
                        event_text,
                        metadata={
                            **base_metadata,
                            **self._internal_prompt_message_metadata(
                                source=normalized_source,
                                internal_prompt_kind="cron_event_bundle",
                                extra={"cron_internal": True, "cron_job_id": cron_job_id},
                            ),
                        },
                    )
            self._loop.sessions.save(persisted_session)
        except Exception:
            logger.debug("Internal prompt transcript persistence skipped for {}", self._state.session_key)
        return persisted_session

    @staticmethod
    def _normalize_web_uploads(uploads: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in list(uploads or []):
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("path") or "").strip()
            if not path:
                continue
            item = {
                "path": path,
                "name": str(raw.get("name") or "").strip() or path,
                "mime_type": str(raw.get("mime_type") or raw.get("mimeType") or "").strip(),
                "kind": str(raw.get("kind") or "").strip(),
            }
            size = raw.get("size")
            if isinstance(size, (int, float)):
                item["size"] = int(size)
            items.append(item)
        return items

    def _pending_user_message_snapshot(self) -> dict[str, Any] | None:
        user_messages = self._pending_user_messages_snapshot()
        if not user_messages:
            return None
        return copy.deepcopy(user_messages[-1])

    def _serialize_visible_user_input_snapshot(
        self,
        user_input: UserInputMessage | Any,
    ) -> dict[str, Any] | None:
        attachments: list[dict[str, Any]] = []
        timestamp: str | None = None
        turn_id = ""
        if isinstance(user_input, UserInputMessage):
            if self._internal_prompt_source(user_input) is not None:
                return None
            metadata = dict(user_input.metadata or {})
            raw_text = metadata.get("web_ceo_raw_text")
            text = str(raw_text) if isinstance(raw_text, str) else self._history_text(user_input.content)
            attachments = self._normalize_web_uploads(metadata.get("web_ceo_uploads"))
            timestamp = user_input.timestamp
            turn_id = str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or "").strip()
        else:
            text = self._history_text(user_input)
        if not text.strip() and not attachments:
            return None
        payload: dict[str, Any] = {"role": "user", "content": text}
        if attachments:
            payload["attachments"] = attachments
        if isinstance(timestamp, str) and timestamp.strip():
            payload["timestamp"] = timestamp.strip()
        if turn_id:
            payload["turn_id"] = turn_id
        return payload

    def _pending_user_messages_snapshot(self) -> list[dict[str, Any]]:
        prompt = self._last_prompt
        fallback_user_input = (
            prompt
            if isinstance(prompt, UserInputMessage) and self._internal_prompt_source(prompt) is None
            else None
        )
        current_batch = self._current_user_batch_inputs(fallback_user_input)
        if current_batch:
            return [
                payload
                for payload in (
                    self._serialize_visible_user_input_snapshot(item)
                    for item in current_batch
                )
                if isinstance(payload, dict)
            ]
        serialized = self._serialize_visible_user_input_snapshot(prompt)
        return [serialized] if isinstance(serialized, dict) else []

    def _internal_prompt_source(self, prompt: Any | None = None) -> str | None:
        current = self._last_prompt if prompt is None else prompt
        if not isinstance(current, UserInputMessage):
            return None
        metadata = dict(current.metadata or {})
        if bool(metadata.get("heartbeat_internal")):
            return "heartbeat"
        if bool(metadata.get("cron_internal")):
            return "cron"
        return None

    def _is_heartbeat_internal_prompt(self, prompt: Any | None = None) -> bool:
        return self._internal_prompt_source(prompt) == "heartbeat"

    def _interaction_flow_snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        default_source = self._internal_prompt_source() or "user"
        for raw in self._event_log:
            if not isinstance(raw, dict):
                continue
            event_type = str(raw.get("type") or "").strip()
            payload = raw.get("payload")
            event_payload = payload if isinstance(payload, dict) else {}
            event_data = event_payload.get("data") if isinstance(event_payload.get("data"), dict) else {}

            def _event_value(key: str) -> Any:
                if key in event_data and event_data.get(key) is not None:
                    return event_data.get(key)
                if key in event_payload and event_payload.get(key) is not None:
                    return event_payload.get(key)
                return None

            if event_type == "tool_execution_update" and bool(event_data.get("watchdog")):
                continue
            if event_type == "tool_execution_start":
                status = "running"
                is_update = False
            elif event_type == "tool_execution_update":
                status = "running"
                is_update = True
            elif event_type == "tool_execution_end":
                status = "error" if bool(event_payload.get("is_error")) else "success"
                is_update = False
            else:
                continue
            items.append(
                {
                    "status": status,
                    "tool_name": str(_event_value("tool_name") or "tool").strip() or "tool",
                    "text": str(event_payload.get("text") or "").strip(),
                    "timestamp": str(raw.get("timestamp") or "").strip(),
                    "tool_call_id": str(_event_value("tool_call_id") or "").strip(),
                    "arguments_text": str("" if _event_value("arguments_text") is None else _event_value("arguments_text")).strip(),
                    "output_text": str("" if _event_value("output_text") is None else _event_value("output_text")).strip(),
                    "output_preview_text": str(
                        "" if _event_value("output_preview_text") is None else _event_value("output_preview_text")
                    ).strip(),
                    "output_ref": str(_event_value("output_ref") or "").strip(),
                    "started_at": str(_event_value("started_at") or "").strip(),
                    "finished_at": str(_event_value("finished_at") or "").strip(),
                    "is_error": bool(event_payload.get("is_error")),
                    "is_update": is_update,
                    "kind": str(event_payload.get("kind") or "").strip(),
                    "source": str(event_payload.get("source") or event_data.get("source") or default_source).strip()
                    or default_source,
                    "recovery_decision": str(_event_value("recovery_decision") or "").strip(),
                    "lost_result_summary": str(_event_value("lost_result_summary") or "").strip(),
                    "related_tool_call_ids": [
                        str(raw_id or "").strip()
                        for raw_id in list(_event_value("related_tool_call_ids") or [])
                        if str(raw_id or "").strip()
                    ],
                    "attempted_tools": [
                        str(raw_name or "").strip()
                        for raw_name in list(_event_value("attempted_tools") or [])
                        if str(raw_name or "").strip()
                    ],
                    "evidence": [
                        dict(entry)
                        for entry in list(_event_value("evidence") or [])
                        if isinstance(entry, dict)
                    ],
                }
            )
            elapsed_seconds = event_data.get("elapsed_seconds", event_payload.get("elapsed_seconds"))
            if isinstance(elapsed_seconds, (int, float)):
                items[-1]["elapsed_seconds"] = float(elapsed_seconds)
        return items

    def _has_renderable_frontdoor_stage_state(self) -> bool:
        stage_state = getattr(self, "_frontdoor_stage_state", None)
        stages = stage_state.get("stages") if isinstance(stage_state, dict) else None
        if not isinstance(stages, list):
            return False
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id") or "").strip()
            rounds = stage.get("rounds")
            if stage_id and isinstance(rounds, list):
                return True
        return False

    def _frontdoor_canonical_context_snapshot(self) -> dict[str, Any]:
        snapshot = normalize_frontdoor_canonical_context(
            copy.deepcopy(getattr(self, "_frontdoor_canonical_context", None) or {})
        )
        return snapshot if list(snapshot.get("stages") or []) else {}

    def _frontdoor_visible_canonical_context_snapshot(self) -> dict[str, Any]:
        snapshot = normalize_frontdoor_canonical_context(
            copy.deepcopy(getattr(self, "_frontdoor_stage_state", None) or {})
        )
        return snapshot if list(snapshot.get("stages") or []) else {}

    def _compression_snapshot(self) -> dict[str, Any]:
        raw = getattr(self, "_compression_state", None)
        if not isinstance(raw, dict):
            return {}
        snapshot = {
            "status": str(raw.get("status") or "").strip(),
            "text": str(raw.get("text") or "").strip(),
            "source": str(raw.get("source") or "").strip(),
            "needs_recheck": bool(raw.get("needs_recheck")),
        }
        if not snapshot["status"] and not snapshot["text"] and not snapshot["source"] and not snapshot["needs_recheck"]:
            return {}
        return snapshot

    async def _flush_memory_review_after_compression(self) -> None:
        """压缩轮末：先冲刷复核窗口，再采纳冻结的长期记忆快照。

        顺序是这套采纳规则的契约本身：`run_due_batch_once()` 会同步应用记忆批次改写
        `MEMORY.md`，先读快照就永远读到旧文档，采纳点等于没接上。
        """
        memory_manager = getattr(self._loop, "memory_manager", None)
        if memory_manager is None:
            return
        try:
            flush_result = await memory_manager.flush_review_window(
                session_key=self._state.session_key,
                trigger_source="token_compression",
            )
            if str(flush_result.get("status") or "").strip() == "queued":
                await memory_manager.run_due_batch_once()
        except Exception:
            await self._emit(
                "message_delta",
                channel="analysis",
                kind="persistence_warning",
                text="Memory compression flush failed; turn history is still available in session transcript.",
            )
        adopt_memory_snapshot(
            self,
            memory_manager=memory_manager,
            reason=MEMORY_SNAPSHOT_ADOPTION_TOKEN_COMPRESSION,
        )

    def append_context_compression_marker(
        self,
        *,
        state: str,
        source: str,
        stats: dict[str, Any] | None = None,
        persisted_session: Any | None = None,
    ) -> bool:
        """在转录末尾落一条上下文压缩区分线，把已折叠的历史与之后的新回合永久分开。

        UI-only：prompt_visible=False 让模型侧看不到它，ui_visible=True 让快照放行。
        传入 persisted_session 时由调用方负责 save（回合收尾本来就是一次整体落盘）。
        """
        normalized_state = str(state or "").strip().lower()
        label = CONTEXT_COMPRESSION_MARKER_LABELS.get(normalized_state)
        if not label:
            return False
        owns_store = persisted_session is None
        store = getattr(self._loop, "sessions", None)
        if owns_store and store is None:
            return False
        session_key = str(self._state.session_key or "").strip()
        if not session_key:
            return False
        try:
            target = persisted_session if persisted_session is not None else store.get_or_create(session_key)
            target.add_message(
                "system",
                label,
                metadata={
                    "kind": CONTEXT_COMPRESSION_MARKER_KIND,
                    "compression_state": normalized_state,
                    "source": str(source or "").strip().lower(),
                    "prompt_visible": False,
                    "ui_visible": True,
                    "stats": dict(stats or {}),
                },
            )
            if owns_store:
                store.save(target)
        except Exception:
            logger.opt(exception=True).warning("Failed to persist context compression marker")
            return False
        return True

    def _begin_frontdoor_compression_generation(self) -> int:
        self._frontdoor_compression_generation_seq = int(self._frontdoor_compression_generation_seq or 0) + 1
        generation_id = self._frontdoor_compression_generation_seq
        self._active_frontdoor_compression_generation = generation_id
        self._cancelled_frontdoor_compression_generations.discard(generation_id)
        return generation_id

    def _finish_frontdoor_compression_generation(self, generation_id: int) -> None:
        try:
            normalized_generation_id = int(generation_id or 0)
        except (TypeError, ValueError):
            normalized_generation_id = 0
        if normalized_generation_id <= 0:
            return
        if self._active_frontdoor_compression_generation == normalized_generation_id:
            self._active_frontdoor_compression_generation = None
        self._cancelled_frontdoor_compression_generations.discard(normalized_generation_id)

    def _cancel_active_frontdoor_compression_generation(self) -> None:
        generation_id = self._active_frontdoor_compression_generation
        if generation_id is None:
            return
        self._frontdoor_pending_shrink_reason = "token_compression"
        self._cancelled_frontdoor_compression_generations.add(generation_id)

    def _is_frontdoor_compression_generation_cancelled(self, generation_id: int) -> bool:
        try:
            normalized_generation_id = int(generation_id or 0)
        except (TypeError, ValueError):
            return False
        if normalized_generation_id <= 0:
            return False
        return normalized_generation_id in self._cancelled_frontdoor_compression_generations

    def frontdoor_inbound_hold(self) -> str:
        """入站车道唯一要问的问题：现在能不能为该会话起一个前门回合。空串=可以。

        不能只问 running：手动压缩跑在回合外，而 `pause(manual=True)` 只在点击那一刻
        会话正在跑时才叫（api/ceo_sessions 的 `pause_first`），所以压缩在途时
        `is_running`/`status` 与空闲会话完全同形。实盘后果：13:05:31 起压缩，13:06:01
        的渠道消息照常起回合，13:06:28 落地的 17,956 tok 摘要在 13:06:46 被该回合用
        30 秒前的种子写的 115,338 tok 基线覆盖，收口水位线选择器一起消失。靠 pause 兜底
        也不解决：渠道侧的回合注册在 `None` 键上，pause 既停不掉它也等不到它
        （见 external_turns 模块 docstring）。

        自动压缩不算 hold：它在回合内部跑，那时本来就有回合在跑，判成 hold 会让自动车道
        自己等自己。"""
        status = str(getattr(self._state, "status", "") or "").strip().lower()
        if bool(getattr(self._state, "is_running", False)) or status == "running":
            return "turn_running"
        manual = getattr(self, MANUAL_COMPRESSION_STATE_ATTR, None)
        if isinstance(manual, dict) and str(manual.get("status") or "").strip().lower() == MANUAL_COMPRESSION_RUNNING:
            return "manual_context_compression"
        return ""

    def reminder_context_snapshot(self) -> dict[str, Any] | None:
        status = str(self._state.status or "").strip().lower()
        if not (self._state.is_running or status in {"running", "paused", "error"}):
            return None
        visible_canonical_context = self._frontdoor_visible_canonical_context_snapshot()
        durable_canonical_context = self._frontdoor_canonical_context_snapshot()
        compression = self._compression_snapshot()
        user_message = self._pending_user_message_snapshot()
        assistant_text = str(self._state.latest_message or "").strip()
        snapshot: dict[str, Any] = {
            "session_key": str(self._state.session_key or "").strip(),
            "turn_id": self._current_turn_id(),
            "source": self._internal_prompt_source() or "user",
            "status": status or ("running" if self._state.is_running else "idle"),
            "user_message": user_message or {},
            "assistant_text": assistant_text,
            "visible_canonical_context": visible_canonical_context,
            "frontdoor_canonical_context": durable_canonical_context,
            "compression": compression,
            "hydrated_tool_names": [
                str(item or "").strip()
                for item in list(getattr(self, "_frontdoor_hydrated_tool_names", []) or [])
                if str(item or "").strip()
            ],
            "frontdoor_selection_debug": copy.deepcopy(getattr(self, "_frontdoor_selection_debug", None) or {}),
            "frontdoor_actual_request_path": str(getattr(self, "_frontdoor_actual_request_path", "") or "").strip(),
            "active_tool_observation": copy.deepcopy(getattr(self, "_latest_sidecar_tool_observation", None) or {}),
        }
        return snapshot

    def _cancel_assistant_stream_flush_task(self) -> None:
        task = self._assistant_stream_flush_task
        self._assistant_stream_flush_task = None
        if task is not None and not task.done():
            task.cancel()

    def _reset_assistant_stream_state(self) -> None:
        self._cancel_assistant_stream_flush_task()
        self._assistant_stream_seq = 0
        self._assistant_stream_pending_text = ""
        self._assistant_stream_last_emitted_text = ""
        self._assistant_stream_last_emit_monotonic = 0.0
        self._assistant_segment_open = False

    def _schedule_assistant_stream_flush(self) -> None:
        task = self._assistant_stream_flush_task
        if task is not None and not task.done():
            return
        self._assistant_stream_flush_task = asyncio.create_task(self._delayed_assistant_stream_flush())

    async def _delayed_assistant_stream_flush(self) -> None:
        try:
            await asyncio.sleep(_ASSISTANT_STREAM_FLUSH_WINDOW_SECONDS)
            await self._flush_assistant_text_delta(force=False)
        except asyncio.CancelledError:
            return
        finally:
            current = asyncio.current_task()
            if self._assistant_stream_flush_task is current:
                self._assistant_stream_flush_task = None

    async def _flush_assistant_text_delta(self, *, force: bool) -> None:
        text = str(self._assistant_stream_pending_text or "")
        if not text:
            return
        if not force and text == self._assistant_stream_last_emitted_text:
            return
        turn_id = self._current_turn_id()
        if not turn_id:
            return
        source = self._internal_prompt_source() or "user"
        self._assistant_stream_seq += 1
        self._assistant_stream_last_emitted_text = text
        self._assistant_stream_pending_text = ""
        try:
            self._assistant_stream_last_emit_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._assistant_stream_last_emit_monotonic = 0.0
        self._sync_persisted_inflight_turn()
        await self._emit(
            "assistant_stream_delta",
            turn_id=turn_id,
            source=source,
            text=text,
            seq=self._assistant_stream_seq,
        )

    async def _handle_assistant_text_delta(self, text: str) -> None:
        normalized_text = str(text or "")
        if not normalized_text:
            return
        if not self._assistant_segment_open:
            self._state.latest_message = ""
            self._assistant_segment_open = True
        current_text = str(self._state.latest_message or "")
        next_text = current_text + normalized_text
        if next_text == current_text:
            return
        self._state.latest_message = next_text
        self._assistant_stream_pending_text = next_text
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            now = 0.0
        if (
            self._assistant_stream_seq <= 0
            or self._assistant_stream_last_emit_monotonic <= 0.0
            or (now - self._assistant_stream_last_emit_monotonic) >= _ASSISTANT_STREAM_FLUSH_WINDOW_SECONDS
        ):
            await self._flush_assistant_text_delta(force=True)
            return
        self._schedule_assistant_stream_flush()

    def _begin_assistant_text_segment(self) -> None:
        # 模型调用开始标记段边界：驻留文本不清空，等新一段首个流式
        # delta 到达再整体覆盖，避免工具执行期间气泡空白。
        self._assistant_segment_open = False

    def manual_pause_waiting_reason(self) -> bool:
        return False

    def _set_manual_pause_waiting_reason(self, enabled: bool) -> None:
        _ = enabled

    def _persisted_manual_pause_waiting_reason(self) -> bool:
        return False

    def _clear_manual_pause_waiting_reason_for_user_turn(self) -> None:
        return

    def _resolve_progress_tool_target(self, data: dict[str, Any]) -> tuple[str, str]:
        tool_name = str(data.get("tool_name") or "").strip()
        tool_call_id = self._event_tool_call_id(data)
        if tool_call_id:
            tool_name = self._pending_tool_call_names.get(tool_call_id, "") or tool_name
        if tool_name and not tool_call_id:
            tool_call_id = self._peek_pending_tool_call_id(tool_name)
        if not tool_name and len(self._pending_tool_call_names) == 1:
            tool_call_id, tool_name = next(iter(self._pending_tool_call_names.items()))
        return self._normalize_tool_name(tool_name), tool_call_id

    def _remember_background_tool_target(self, *, execution_id: str, tool_name: str, tool_call_id: str) -> None:
        key = str(execution_id or "").strip()
        if not key:
            return
        self._background_tool_targets[key] = {
            "tool_name": str(tool_name or "tool").strip() or "tool",
            "tool_call_id": str(tool_call_id or "").strip(),
        }

    def _forget_background_tool_target(self, execution_id: str) -> None:
        self._background_tool_targets.pop(str(execution_id or "").strip(), None)

    def _resolve_control_tool_target(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        execution_id = str((payload or {}).get("execution_id") or "").strip()
        mapped = self._background_tool_targets.get(execution_id, {})
        target_tool_name = str(mapped.get("tool_name") or "").strip()
        target_tool_call_id = str(mapped.get("tool_call_id") or "").strip()
        if not target_tool_name:
            target_tool_name = str((payload or {}).get("tool_name") or "").strip()
        if not target_tool_call_id and target_tool_name:
            target_tool_call_id = self._peek_pending_tool_call_id(target_tool_name)
        return (
            self._normalize_tool_name(target_tool_name or tool_name),
            target_tool_call_id,
            execution_id,
        )

    def _build_execution_context_snapshot(
        self,
        *,
        allow_manual_pause: bool = False,
        status_override: str | None = None,
    ) -> dict[str, Any] | None:
        if not allow_manual_pause and self.manual_pause_waiting_reason():
            return None
        status = str(status_override or self._state.status or "").strip().lower()
        if self._state.is_running and status not in {"paused", "error", "completed"}:
            status = "running"
        if not (self._state.is_running or status in {"running", "paused", "error"}):
            return None
        canonical_context = self._frontdoor_visible_canonical_context_snapshot()
        frontdoor_canonical_context = self._frontdoor_canonical_context_snapshot()
        compression = self._compression_snapshot()
        frontdoor_stage_state = (
            copy.deepcopy(getattr(self, "_frontdoor_stage_state", None) or {})
            if self._has_renderable_frontdoor_stage_state()
            else {}
        )
        snapshot: dict[str, Any] = {
            "status": status or ("running" if self._state.is_running else "idle"),
            "compression": compression,
            "execution_trace_summary": copy.deepcopy(canonical_context) if canonical_context else {},
        }
        if canonical_context:
            snapshot["canonical_context"] = canonical_context
        if frontdoor_stage_state:
            snapshot["frontdoor_stage_state"] = frontdoor_stage_state
        if frontdoor_canonical_context:
            snapshot["frontdoor_canonical_context"] = frontdoor_canonical_context
        turn_id = self._current_turn_id()
        if turn_id:
            snapshot["turn_id"] = turn_id
        turn_usage = (getattr(self, "_frontdoor_turn_usage", None) or {}).get(turn_id)
        if turn_usage:
            snapshot["usage"] = copy.deepcopy(turn_usage)
        prompt = self._last_prompt
        prompt_source = self._internal_prompt_source(prompt)
        if prompt_source is not None:
            snapshot["source"] = prompt_source
        user_messages = self._pending_user_messages_snapshot()
        if user_messages:
            snapshot["user_messages"] = copy.deepcopy(user_messages)
        user_message = self._pending_user_message_snapshot()
        if user_message is not None:
            snapshot["user_message"] = user_message
        if self._state.latest_message:
            snapshot["assistant_text"] = str(self._state.latest_message)
        if self._state.last_error is not None:
            snapshot["last_error"] = asdict(self._state.last_error)
        hydrated_tool_names = [
            str(item or "").strip()
            for item in list(getattr(self, "_frontdoor_hydrated_tool_names", []) or [])
            if str(item or "").strip()
        ]
        if hydrated_tool_names:
            snapshot["hydrated_tool_names"] = hydrated_tool_names
        repair_required_tool_items = [
            dict(item)
            for item in list(getattr(self, "_frontdoor_repair_required_tool_items", []) or [])
            if isinstance(item, dict)
        ]
        if repair_required_tool_items:
            snapshot["repair_required_tool_items"] = repair_required_tool_items
        repair_required_skill_items = [
            dict(item)
            for item in list(getattr(self, "_frontdoor_repair_required_skill_items", []) or [])
            if isinstance(item, dict)
        ]
        if repair_required_skill_items:
            snapshot["repair_required_skill_items"] = repair_required_skill_items
        from g3ku.runtime.web_ceo_sessions import strip_multimodal_blocks_from_message_records

        frontdoor_request_body_messages = strip_multimodal_blocks_from_message_records(
            [
                dict(item)
                for item in list(getattr(self, "_frontdoor_request_body_messages", []) or [])
                if isinstance(item, dict)
            ]
        )
        if frontdoor_request_body_messages:
            snapshot["frontdoor_request_body_messages"] = frontdoor_request_body_messages
        frontdoor_history_shrink_reason = str(
            getattr(self, "_frontdoor_history_shrink_reason", "") or ""
        ).strip()
        if frontdoor_history_shrink_reason:
            snapshot["frontdoor_history_shrink_reason"] = frontdoor_history_shrink_reason
        frontdoor_token_preflight_diagnostics = copy.deepcopy(
            getattr(self, "_frontdoor_token_preflight_diagnostics", None) or {}
        )
        if frontdoor_token_preflight_diagnostics:
            snapshot["frontdoor_token_preflight_diagnostics"] = frontdoor_token_preflight_diagnostics
        model_retry_status = getattr(self, "_frontdoor_model_retry_status", None)
        if (
            isinstance(model_retry_status, dict)
            and str(model_retry_status.get("state") or "").strip() == "retrying"
        ):
            snapshot["model_retry_status"] = copy.deepcopy(model_retry_status)
        frontdoor_selection_debug = getattr(self, "_frontdoor_selection_debug", None)
        if isinstance(frontdoor_selection_debug, dict) and frontdoor_selection_debug:
            snapshot["frontdoor_selection_debug"] = copy.deepcopy(frontdoor_selection_debug)
        actual_request_path = str(getattr(self, "_frontdoor_actual_request_path", "") or "").strip()
        if actual_request_path:
            snapshot["actual_request_path"] = actual_request_path
        prompt_cache_key_hash = str(getattr(self, "_frontdoor_prompt_cache_key_hash", "") or "").strip()
        if prompt_cache_key_hash:
            snapshot["prompt_cache_key_hash"] = prompt_cache_key_hash
        actual_request_hash = str(getattr(self, "_frontdoor_actual_request_hash", "") or "").strip()
        if actual_request_hash:
            snapshot["actual_request_hash"] = actual_request_hash
        actual_request_message_count = int(getattr(self, "_frontdoor_actual_request_message_count", 0) or 0)
        if actual_request_message_count:
            snapshot["actual_request_message_count"] = actual_request_message_count
        actual_tool_schema_hash = str(getattr(self, "_frontdoor_actual_tool_schema_hash", "") or "").strip()
        if actual_tool_schema_hash:
            snapshot["actual_tool_schema_hash"] = actual_tool_schema_hash
        actual_request_history = getattr(self, "_frontdoor_actual_request_history", None)
        if isinstance(actual_request_history, list) and actual_request_history:
            snapshot["actual_request_history"] = copy.deepcopy(actual_request_history)
        frontdoor_restore_source = str(getattr(self, "_frontdoor_restore_source", "none") or "none").strip() or "none"
        if frontdoor_restore_source != "none":
            snapshot["frontdoor_restore_source"] = frontdoor_restore_source
        frontdoor_baseline_sync_decision = str(
            getattr(self, "_frontdoor_baseline_sync_decision", "") or ""
        ).strip()
        if frontdoor_baseline_sync_decision:
            snapshot["frontdoor_baseline_sync_decision"] = frontdoor_baseline_sync_decision
        if (
            not canonical_context
            and not compression
            and "turn_id" not in snapshot
            and "source" not in snapshot
            and "user_message" not in snapshot
            and "assistant_text" not in snapshot
            and "last_error" not in snapshot
            and "frontdoor_stage_state" not in snapshot
            and "frontdoor_canonical_context" not in snapshot
            and "hydrated_tool_names" not in snapshot
            and "repair_required_tool_items" not in snapshot
            and "repair_required_skill_items" not in snapshot
            and "frontdoor_request_body_messages" not in snapshot
            and "frontdoor_history_shrink_reason" not in snapshot
            and "frontdoor_token_preflight_diagnostics" not in snapshot
            and "model_retry_status" not in snapshot
            and "frontdoor_selection_debug" not in snapshot
            and "actual_request_path" not in snapshot
            and "prompt_cache_key_hash" not in snapshot
            and "actual_request_hash" not in snapshot
            and "actual_request_message_count" not in snapshot
            and "actual_tool_schema_hash" not in snapshot
            and "actual_request_history" not in snapshot
        ):
            return None
        return snapshot

    def _current_inflight_turn_snapshot(self) -> dict[str, Any] | None:
        return self._build_execution_context_snapshot()

    def preserved_inflight_turn_snapshot(self) -> dict[str, Any] | None:
        if self._preserved_inflight_turn is None:
            return None
        return copy.deepcopy(self._preserved_inflight_turn)

    def inflight_turn_snapshot(self) -> dict[str, Any] | None:
        if self.manual_pause_waiting_reason():
            return None
        snapshot = self._current_inflight_turn_snapshot()
        if snapshot is not None:
            return snapshot
        if self._preserved_inflight_turn is not None:
            return copy.deepcopy(self._preserved_inflight_turn)
        return None

    def clear_preserved_inflight_turn(self) -> None:
        if self._preserved_inflight_turn is None:
            return
        self._preserved_inflight_turn = None
        self._sync_persisted_inflight_turn()

    def has_blocking_tool_execution(self) -> bool:
        return bool(self._background_tool_targets)

    def clear_blocking_tool_execution(self, execution_id: str) -> None:
        self._forget_background_tool_target(execution_id)

    @staticmethod
    def _parse_progress_payload(content: Any) -> dict[str, Any] | None:
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text or text[:1] not in {"{", "["}:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _register_pending_tool_call(self, tool_name: str, data: dict[str, Any] | None = None) -> str:
        normalized = self._normalize_tool_name(tool_name)
        call_id = self._event_tool_call_id(data)
        if not call_id:
            self._tool_seq += 1
            call_id = f"{normalized}:{self._tool_seq}"
        else:
            self._discard_pending_tool_call(call_id)
        self._pending_tool_call_names[call_id] = normalized
        self._pending_tool_name_calls.setdefault(normalized, deque()).append(call_id)
        return call_id

    def _resolve_completed_tool_call(self, tool_name: str, data: dict[str, Any] | None = None) -> tuple[str, str]:
        normalized = self._normalize_tool_name(tool_name)
        explicit_call_id = self._event_tool_call_id(data)
        if explicit_call_id:
            resolved_name = self._pending_tool_call_names.get(explicit_call_id, normalized)
            self._discard_pending_tool_call(explicit_call_id)
            return resolved_name or normalized, explicit_call_id
        fallback_call_id = self._peek_pending_tool_call_id(normalized)
        if fallback_call_id:
            self._discard_pending_tool_call(fallback_call_id)
            return normalized, fallback_call_id
        if not tool_name and len(self._pending_tool_call_names) == 1:
            only_call_id, only_tool_name = next(iter(self._pending_tool_call_names.items()))
            self._discard_pending_tool_call(only_call_id)
            return only_tool_name, only_call_id
        return normalized, f"{normalized}:{self._tool_seq + 1}"

    async def _emit(self, event_type: str, **payload):
        event = AgentEvent(type=event_type, timestamp=self._now(), payload=payload)
        self._state.event_count += 1
        self._event_log.append({"type": event.type, "timestamp": event.timestamp, "payload": dict(event.payload)})
        for listener in list(self._listeners):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result
        return event

    def _sync_persisted_inflight_turn(self) -> None:
        session_key = str(self._state.session_key or "").strip()
        if not session_key.startswith("web:"):
            return
        try:
            from g3ku.runtime.web_ceo_sessions import (
                is_restorable_inflight_turn_snapshot,
                write_inflight_turn_snapshot,
            )

            snapshot = self.inflight_turn_snapshot()
            if not is_restorable_inflight_turn_snapshot(snapshot):
                snapshot = None
            write_inflight_turn_snapshot(session_key, snapshot)
        except Exception:
            logger.debug("Skipped persisted inflight turn sync for {}", session_key)

    def _sync_persisted_paused_execution_context(self) -> None:
        session_key = str(self._state.session_key or "").strip()
        if not session_key.startswith("web:"):
            return
        try:
            from g3ku.runtime.web_ceo_sessions import (
                is_restorable_inflight_turn_snapshot,
                write_paused_execution_context,
            )

            snapshot = copy.deepcopy(self._paused_execution_context)
            if not is_restorable_inflight_turn_snapshot(snapshot):
                snapshot = None
            write_paused_execution_context(session_key, snapshot)
        except Exception:
            logger.debug("Skipped paused execution context sync for {}", session_key)

    async def _persist_turn_transcript(
        self,
        *,
        user_input: UserInputMessage,
        user_text: str,
        assistant_text: str,
        interaction_flow: list[dict[str, Any]],
        internal_source: str | None,
        route_kind: str,
        assistant_metadata: dict[str, Any] | None = None,
        retire_lingering_transcript_rows: bool = False,
    ) -> Any | None:
        persisted_session = None
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            if internal_source is None:
                visible_user_texts: list[str] = []
                for current_input in self._current_user_batch_inputs(user_input):
                    current_text = self._history_text(current_input.content)
                    if not current_text.strip() and not current_input.attachments:
                        continue
                    self._upsert_transcript_user_message(
                        persisted_session=persisted_session,
                        user_input=current_input,
                        user_text=current_text,
                        transcript_state=_TRANSCRIPT_STATE_COMPLETED,
                    )
                    visible_user_texts.append(current_text)
                if visible_user_texts:
                    user_text = visible_user_texts[-1]
                if retire_lingering_transcript_rows:
                    # 仅在正常完成路径清理残留 paused 条目；错误路径的请求体未必完成
                    # 基线回写，提前退役会让暂停消息从模型上下文永久消失。
                    self._complete_lingering_paused_user_messages(persisted_session)
            if retire_lingering_transcript_rows:
                # 排队行的退役对用户可见与内部回合都要做：上面整段被 internal 分支跳过，
                # 而心跳/cron 回合同样会在 prepare 阶段消费排队的 follow-up。
                self._retire_consumed_pending_user_messages(persisted_session)
            assistant_payload: dict[str, Any] = {}
            canonical_context = self._frontdoor_visible_canonical_context_snapshot()
            compression = self._compression_snapshot()
            if canonical_context:
                projected = _project_transcript_canonical_context(canonical_context)
                if projected:
                    assistant_payload.update(
                        plan_transcript_cc_row(getattr(persisted_session, "messages", None), projected)
                    )
            if compression:
                assistant_payload["compression"] = compression
            metadata_payload = dict(assistant_metadata or {})
            if internal_source is not None:
                metadata_payload.setdefault("source", internal_source)
            verified_task_ids = self._normalize_verified_task_ids(self._last_verified_task_ids)
            if verified_task_ids:
                metadata_payload["task_ids"] = verified_task_ids
            turn_id = self._current_turn_id(user_input)
            if turn_id:
                assistant_payload["turn_id"] = turn_id
                # 轮次 token 用量随 transcript 持久化：frontdoor 请求工件会被修剪，
                # transcript 级 usage 才是历史气泡悬停展示的稳定数据源。
                turn_usage = (getattr(self, "_frontdoor_turn_usage", None) or {}).get(turn_id)
                if isinstance(turn_usage, dict) and any(
                    int(turn_usage.get(field) or 0)
                    for field in ("input_tokens", "output_tokens", "cache_hit_tokens")
                ):
                    assistant_payload["usage"] = {
                        "input_tokens": int(turn_usage.get("input_tokens") or 0),
                        "output_tokens": int(turn_usage.get("output_tokens") or 0),
                        "cache_hit_tokens": int(turn_usage.get("cache_hit_tokens") or 0),
                        "call_count": int(turn_usage.get("call_count") or 0),
                    }
            if metadata_payload:
                assistant_payload["metadata"] = metadata_payload
            persisted_session.add_message("assistant", assistant_text, **assistant_payload)
            # 发送时（自动）压缩在收尾处落线：区分线以上是这轮被折进摘要的历史。
            if str(getattr(self, "_frontdoor_compressed_turn_id", "") or "").strip() == turn_id and turn_id:
                setattr(self, "_frontdoor_compressed_turn_id", "")
                self.append_context_compression_marker(
                    state="completed",
                    source="auto",
                    persisted_session=persisted_session,
                )
            if self._state.session_key.startswith("web:"):
                from g3ku.runtime.web_ceo_sessions import update_ceo_session_after_turn

                update_ceo_session_after_turn(
                    persisted_session,
                    user_text="" if internal_source is not None else user_text,
                    assistant_text=assistant_text,
                    route_kind=str(route_kind or ""),
                )
            self._loop.sessions.save(persisted_session)
        except Exception:
            await self._emit(
                "message_delta",
                channel="analysis",
                kind="persistence_warning",
                text="Session transcript persistence failed; response is still available in-memory.",
            )
        return persisted_session

    async def _emit_state_snapshot(self):
        self._sync_persisted_inflight_turn()
        await self._emit("state_snapshot", state=self.state_dict())

    async def _persist_manual_pause_user_messages(self) -> None:
        user_inputs = [
            item
            for item in self._current_user_batch_inputs(
                self._last_prompt if isinstance(self._last_prompt, UserInputMessage) else None
            )
            if isinstance(item, UserInputMessage)
        ]
        if not user_inputs:
            prompt = self._last_prompt
            fallback = (
                prompt
                if isinstance(prompt, UserInputMessage)
                else UserInputMessage(content=self._history_text(prompt))
            )
            user_inputs = [fallback]
        if any(self._internal_prompt_source(item) is not None for item in user_inputs):
            return
        visible_texts = [
            self._history_text(item.content)
            for item in user_inputs
            if self._history_text(item.content).strip() or item.attachments
        ]
        if not visible_texts and not any(item.attachments for item in user_inputs):
            return
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            for item in user_inputs:
                user_text = self._history_text(item.content)
                if not user_text.strip() and not item.attachments:
                    continue
                self._upsert_transcript_user_message(
                    persisted_session=persisted_session,
                    user_input=item,
                    user_text=user_text,
                    transcript_state=_TRANSCRIPT_STATE_PAUSED,
                )
            if self._state.session_key.startswith("web:"):
                from g3ku.runtime.web_ceo_sessions import update_ceo_session_after_turn

                update_ceo_session_after_turn(
                    persisted_session,
                    user_text=visible_texts[-1] if visible_texts else "",
                    assistant_text="",
                    route_kind="",
                )
            self._loop.sessions.save(persisted_session)
        except Exception:
            await self._emit(
                "message_delta",
                channel="analysis",
                kind="persistence_warning",
                text="Manual pause transcript persistence failed; the paused user message is still available in-memory.",
            )

    async def _handle_progress(
        self,
        content: str,
        *,
        tool_hint: bool = False,
        deep_progress: bool = False,
        event_kind: str | None = None,
        event_data=None,
    ) -> None:
        kind = event_kind or ("tool_plan" if tool_hint else "deep_progress" if deep_progress else "progress")
        data = event_data if isinstance(event_data, dict) else {}
        tool_name = str(data.get("tool_name") or "").strip() or "tool"
        source = self._internal_prompt_source() or "user"
        observation = dict(data.get("sidecar_observation") or {}) if isinstance(data.get("sidecar_observation"), dict) else {}
        if observation:
            observation["tool_name"] = str(observation.get("tool_name") or tool_name).strip() or tool_name
            self._latest_sidecar_tool_observation = observation

        if kind == "tool_start":
            await self._flush_assistant_text_delta(force=True)
            if tool_name in _LEGACY_CONTROL_TOOL_NAMES:
                return
            call_id = self._register_pending_tool_call(tool_name, data)
            self._state.pending_tool_calls.add(call_id)
            await self._emit(
                "tool_execution_start",
                tool_name=tool_name,
                tool_call_id=call_id,
                text=str(content or ""),
                kind=kind,
                source=source,
                data=data,
            )
            await self._emit_state_snapshot()
            return

        if kind == "tool_result":
            payload = self._parse_progress_payload(content)
            payload_status = str((payload or {}).get("status") or "").strip().lower()
            if payload_status == "background_running":
                if tool_name in _LEGACY_CONTROL_TOOL_NAMES:
                    resolved_tool_name, call_id, execution_id = self._resolve_control_tool_target(
                        tool_name=tool_name,
                        payload=payload,
                    )
                else:
                    resolved_tool_name, call_id = self._resolve_progress_tool_target(data)
                    execution_id = str((payload or {}).get("execution_id") or "").strip()
                if execution_id:
                    self._remember_background_tool_target(
                        execution_id=execution_id,
                        tool_name=resolved_tool_name,
                        tool_call_id=call_id,
                    )
                self._enqueue_background_tool_heartbeat(payload=payload, tool_name=resolved_tool_name)
                await self._emit(
                    "tool_execution_update",
                    kind="tool_background",
                    tool_name=resolved_tool_name,
                    tool_call_id=call_id,
                    text=str(content or ""),
                    source=source,
                    data=data,
                )
                await self._emit_state_snapshot()
                return
            if tool_name in _LEGACY_CONTROL_TOOL_NAMES:
                resolved_tool_name, call_id, execution_id = self._resolve_control_tool_target(
                    tool_name=tool_name,
                    payload=payload,
                )
                if execution_id and payload_status in {"completed", "stopped", "failed", "error", "not_found", "unavailable"}:
                    self._forget_background_tool_target(execution_id)
                if call_id:
                    self._state.pending_tool_calls.discard(call_id)
                await self._emit(
                    "tool_execution_end",
                    tool_name=resolved_tool_name,
                    tool_call_id=call_id,
                    text=str(content or ""),
                    kind=kind,
                    is_error=payload_status in {"stopped", "failed", "error", "not_found", "unavailable"},
                    source=source,
                    data=data,
                )
                await self._emit_state_snapshot()
                return
            tool_name, call_id = self._resolve_completed_tool_call(tool_name, data)
            self._state.pending_tool_calls.discard(call_id)
            if self._latest_sidecar_tool_observation and str(self._latest_sidecar_tool_observation.get("tool_name") or "").strip() == tool_name:
                self._latest_sidecar_tool_observation = {}
            await self._emit(
                "tool_execution_end",
                tool_name=tool_name,
                tool_call_id=call_id,
                text=str(content or ""),
                kind=kind,
                is_error=False,
                source=source,
                data=data,
            )
            await self._emit_state_snapshot()
            return

        if kind == "tool_error":
            if tool_name in _LEGACY_CONTROL_TOOL_NAMES:
                payload = self._parse_progress_payload(content)
                resolved_tool_name, call_id, execution_id = self._resolve_control_tool_target(
                    tool_name=tool_name,
                    payload=payload,
                )
                if execution_id:
                    self._forget_background_tool_target(execution_id)
                if call_id:
                    self._state.pending_tool_calls.discard(call_id)
                error = StructuredError(
                    code="tool_error",
                    message=str(content or f"{resolved_tool_name} failed"),
                    recoverable=True,
                    source="tool",
                    details={"tool_name": resolved_tool_name, "tool_call_id": call_id, **data},
                )
                self._state.last_error = error
                await self._emit(
                    "tool_execution_end",
                    tool_name=resolved_tool_name,
                    tool_call_id=call_id,
                    text=error.message,
                    kind=kind,
                    is_error=True,
                    source=source,
                    data=data,
                )
                await self._emit(
                    "error",
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                    source=error.source,
                    details=error.details,
                )
                await self._emit_state_snapshot()
                return
            tool_name, call_id = self._resolve_completed_tool_call(tool_name, data)
            self._state.pending_tool_calls.discard(call_id)
            if self._latest_sidecar_tool_observation and str(self._latest_sidecar_tool_observation.get("tool_name") or "").strip() == tool_name:
                self._latest_sidecar_tool_observation = observation or {}
            error = StructuredError(
                code="tool_error",
                message=str(content or f"{tool_name} failed"),
                recoverable=True,
                source="tool",
                details={"tool_name": tool_name, "tool_call_id": call_id, **data},
            )
            self._state.last_error = error
            await self._emit(
                "tool_execution_end",
                tool_name=tool_name,
                tool_call_id=call_id,
                text=error.message,
                kind=kind,
                is_error=True,
                source=source,
                data=data,
            )
            await self._emit(
                "error",
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
                source=error.source,
                details=error.details,
            )
            await self._emit_state_snapshot()
            return

        if kind in {"tool_plan", "browser_runtime_bootstrap", "browser_command_status", "tool"}:
            resolved_tool_name, call_id = self._resolve_progress_tool_target(data)
            await self._emit(
                "tool_execution_update",
                kind=kind,
                tool_name=resolved_tool_name,
                tool_call_id=call_id,
                text=str(content or ""),
                source=source,
                data=data,
            )
            return

        if kind == "analysis":
            # 进行中的思考气泡只显示最新一段，避免跨迭代累加堆叠。
            text = str(content or "").strip()
            current = str(self._state.latest_message or "")
            if text and text != current:
                self._state.latest_message = text
                self._assistant_stream_pending_text = text
                self._assistant_stream_last_emitted_text = ""
                self._assistant_segment_open = False
                await self._emit_state_snapshot()

        channel = "analysis" if kind == "analysis" else "deep_progress" if (deep_progress or kind == "deep_progress") else "progress"
        await self._emit(
            "message_delta",
            channel=channel,
            kind=kind,
            text=str(content or ""),
            data=data,
        )

    def _enqueue_background_tool_heartbeat(self, *, payload: dict[str, Any] | None, tool_name: str) -> None:
        heartbeat = getattr(self._loop, "web_session_heartbeat", None)
        if heartbeat is None or not hasattr(heartbeat, "enqueue_tool_background"):
            return
        session_key = str(self._state.session_key or "").strip()
        execution_id = str((payload or {}).get("execution_id") or "").strip()
        if not session_key or not execution_id:
            return
        handoff_payload = dict(payload or {})
        handoff_payload["tool_name"] = str(handoff_payload.get("tool_name") or tool_name or "tool").strip() or "tool"
        try:
            heartbeat.enqueue_tool_background(session_id=session_key, payload=handoff_payload)
        except Exception:
            logger.debug("Background tool heartbeat enqueue skipped for {}", session_key)

    async def _run_message(self, user_input: UserInputMessage) -> str:
        self._multi_agent_runner = getattr(self._loop, "multi_agent_runner", None)
        if self._multi_agent_runner is None:
            raise RuntimeError("Main frontdoor runtime is required but was not initialized.")
        return await self._multi_agent_runner.run_turn(
            user_input=user_input,
            session=self,
            on_progress=self._handle_progress,
        )

    async def _pause_for_frontdoor_interrupt(self, exc: CeoFrontdoorInterrupted) -> RunResult:
        serialized_interrupts = self._serialize_pending_interrupts(exc.interrupts)
        interrupt_values = dict(exc.values or {}) if isinstance(exc.values, dict) else {}
        frontdoor_stage_state = interrupt_values.get("frontdoor_stage_state")
        frontdoor_canonical_context = interrupt_values.get("frontdoor_canonical_context")
        compression_state = interrupt_values.get("compression_state")
        hydrated_tool_names = interrupt_values.get("hydrated_tool_names")
        repair_required_tool_items = interrupt_values.get("repair_required_tool_items")
        repair_required_skill_items = interrupt_values.get("repair_required_skill_items")
        frontdoor_selection_debug = interrupt_values.get("frontdoor_selection_debug")
        preserved_frontdoor_stage_state = getattr(self, "_frontdoor_stage_state", None)
        preserved_frontdoor_canonical_context = getattr(self, "_frontdoor_canonical_context", None)
        preserved_compression_state = getattr(self, "_compression_state", None)
        preserved_hydrated_tool_names = getattr(self, "_frontdoor_hydrated_tool_names", None)
        preserved_repair_required_tool_items = getattr(self, "_frontdoor_repair_required_tool_items", None)
        preserved_repair_required_skill_items = getattr(self, "_frontdoor_repair_required_skill_items", None)
        preserved_frontdoor_selection_debug = getattr(self, "_frontdoor_selection_debug", None)
        self._frontdoor_stage_state = (
            dict(frontdoor_stage_state)
            if isinstance(frontdoor_stage_state, dict)
            else dict(preserved_frontdoor_stage_state)
            if isinstance(preserved_frontdoor_stage_state, dict)
            else {}
        )
        self._frontdoor_canonical_context = normalize_frontdoor_canonical_context(
            frontdoor_canonical_context
            if isinstance(frontdoor_canonical_context, dict)
            else preserved_frontdoor_canonical_context
            if isinstance(preserved_frontdoor_canonical_context, dict)
            else default_frontdoor_canonical_context()
        )
        self._compression_state = (
            dict(compression_state)
            if isinstance(compression_state, dict)
            else dict(preserved_compression_state)
            if isinstance(preserved_compression_state, dict)
            else {}
        )
        self._semantic_context_state = {}
        self._frontdoor_hydrated_tool_names = [
            str(item or "").strip()
            for item in list(hydrated_tool_names or preserved_hydrated_tool_names or [])
            if str(item or "").strip()
        ]
        self._frontdoor_repair_required_tool_items = [
            dict(item)
            for item in list(repair_required_tool_items or preserved_repair_required_tool_items or [])
            if isinstance(item, dict)
        ]
        self._frontdoor_repair_required_skill_items = [
            dict(item)
            for item in list(repair_required_skill_items or preserved_repair_required_skill_items or [])
            if isinstance(item, dict)
        ]
        self._frontdoor_selection_debug = (
            dict(frontdoor_selection_debug)
            if isinstance(frontdoor_selection_debug, dict)
            else dict(preserved_frontdoor_selection_debug)
            if isinstance(preserved_frontdoor_selection_debug, dict)
            else {}
        )
        self._state.is_running = False
        self._state.paused = True
        self._state.status = "paused"
        self._state.latest_message = ""
        self._reset_assistant_stream_state()
        self._state.last_error = None
        self._state.pending_tool_calls.clear()
        self._pending_tool_call_names.clear()
        self._pending_tool_name_calls.clear()
        self._background_tool_targets.clear()
        self._state.pending_interrupts = serialized_interrupts
        self._set_paused_execution_context(
            {
                **(self._build_execution_context_snapshot(allow_manual_pause=True, status_override="paused") or {}),
                "source": "approval",
                "interrupts": serialized_interrupts,
                "graph_state": {"version": 2, "state": dict(getattr(exc, "resume_state", None) or {})},
            }
        )
        await self._emit("frontdoor_interrupt", interrupts=serialized_interrupts)
        await self._emit_state_snapshot()
        return RunResult(output="", events=list(self._event_log))

    @staticmethod
    def _sanitized_error_log_filename_fragment(session_key: str) -> str:
        sanitized = re.sub(r"[:/\\]+", "-", str(session_key or "").strip()).strip("-_ ")
        if not sanitized:
            sanitized = "unknown"
        return sanitized

    @staticmethod
    def _last_tool_interaction_summary(interaction_flow: list[dict[str, Any]] | None) -> str:
        items = [item for item in list(interaction_flow or []) if isinstance(item, dict)]
        if not items:
            return ""
        latest = items[-1]
        parts = [
            f"tool_name={str(latest.get('tool_name') or '').strip() or 'tool'}",
            f"status={str(latest.get('status') or '').strip() or 'unknown'}",
        ]
        for key in ("tool_call_id", "arguments_text", "output_preview_text"):
            value = latest.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if len(text) > 240:
                text = text[:240] + "..."
            parts.append(f"{key}={text}")
        is_error = latest.get("is_error")
        if is_error is not None:
            parts.append(f"is_error={bool(is_error)}")
        return " | ".join(parts)

    def _resolve_runtime_error_dir(self) -> Path:
        """Resolve the ``.g3ku/errors`` directory for the active runtime workspace.

        Prefers the loop's real workspace (production ``AgentRuntimeEngine``),
        then the session manager's workspace, then the configured workspace path,
        and finally the current working directory. Never falls back to the source
        tree, so error logs follow the runtime workspace instead of the repo.
        Best-effort: any failure degrades to the next candidate.
        """
        loop = getattr(self, "_loop", None)
        candidates = (
            getattr(loop, "workspace", None),
            getattr(getattr(loop, "sessions", None), "workspace", None),
            getattr(getattr(loop, "app_config", None), "workspace_path", None),
        )
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return Path(candidate) / ".g3ku" / "errors"
            except (TypeError, ValueError):
                continue
        return Path.cwd() / ".g3ku" / "errors"

    @staticmethod
    def _turn_failed_reply(error_message: str) -> str:
        detail = str(error_message or "").strip() or TURN_FAILED_FRIENDLY_TEXT
        return f"这一轮处理失败：{detail}"

    def _persist_runtime_error_file(
        self,
        exc: Exception,
        *,
        user_text: str,
        interaction_flow: list[dict[str, Any]] | None,
        internal_source: str | None,
        route_kind: str,
    ) -> None:
        """Best-effort persistence of an unhandled turn exception to .g3ku/errors/.

        Writes timestamp / session_key / route_kind / internal_source / exception
        type+message / user_text / last tool interaction summary plus the full
        traceback. Never raises: a failed write must not disturb the reply flow.
        """
        if not isinstance(exc, BaseException):
            return
        try:
            import traceback as _traceback

            session_key = str(self._state.session_key or "").strip() or "unknown"
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            filename_fragment = self._sanitized_error_log_filename_fragment(session_key)
            error_dir = self._resolve_runtime_error_dir()
            error_dir.mkdir(parents=True, exist_ok=True)
            target = error_dir / f"{timestamp}-{filename_fragment}.log"
            traceback_text = "".join(
                _traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).rstrip()
            summary = self._last_tool_interaction_summary(interaction_flow)
            lines = [
                f"timestamp={self._now()}",
                f"session_key={session_key}",
                f"route_kind={str(route_kind or '').strip()}",
                f"internal_source={str(internal_source or 'user').strip()}",
                f"exception_type={exc.__class__.__module__}.{exc.__class__.__qualname__}",
                f"exception_message={str(exc).strip()}",
                f"user_text={str(user_text or '').strip()}",
            ]
            if summary:
                lines.append(f"last_tool_interaction={summary}")
            target.write_text("\n".join(lines) + "\n\n" + traceback_text + "\n", encoding="utf-8")
            logger.info("Persisted runtime error for {} to {}", session_key, str(target))
        except Exception:
            logger.debug(
                "Failed to persist runtime error log for {}",
                str(getattr(self._state, "session_key", "") or "")[:80],
            )

    async def _prompt_locked(
        self,
        user_input: UserInputMessage,
        *,
        persist_transcript: bool = True,
        live_context: dict[str, str] | None = None,
    ) -> RunResult:
        from g3ku.shells.web import refresh_web_agent_runtime

        self._apply_live_context(live_context)
        await refresh_web_agent_runtime(force=False, reason="prompt")
        internal_source = self._internal_prompt_source(user_input)
        heartbeat_internal = internal_source == "heartbeat"
        cron_internal = internal_source == "cron"
        reset_frontdoor_turn_state = internal_source in {None, "heartbeat", "cron"}
        if internal_source is None:
            await self._archive_paused_execution_context_for_ui_history()
            self._clear_manual_pause_waiting_reason_for_user_turn()
            if not self._active_user_batch_inputs or self._active_user_batch_inputs[-1] is not user_input:
                self._configure_user_batch([user_input])
        else:
            self._clear_user_batch_context()
        if internal_source is not None:
            current_snapshot = self._current_inflight_turn_snapshot()
            current_source = str((current_snapshot or {}).get("source") or "").strip().lower()
            if current_snapshot is not None and current_source != internal_source:
                self._preserved_inflight_turn = copy.deepcopy(current_snapshot)
        else:
            self._preserved_inflight_turn = None
        cancel_token = self._loop.create_session_cancellation_token(self._state.session_key)
        self._active_cancel_token = cancel_token
        try:
            self._ensure_user_turn_id(user_input)
            if internal_source is None:
                self._ensure_user_batch_id(user_input)
            self._last_prompt = user_input
            self._event_log = []
            self._last_stop_reason = ""
            self._pending_tool_call_names.clear()
            self._pending_tool_name_calls.clear()
            self._background_tool_targets.clear()
            if reset_frontdoor_turn_state:
                # Fresh visible turns and internal heartbeat/cron turns each start from
                # their own frontdoor runtime window.
                if internal_source is None:
                    self._preserve_frontdoor_actual_request_trace_for_next_visible_turn()
                self._frontdoor_selection_debug = {}
                self._frontdoor_actual_request_path = ""
                self._frontdoor_actual_request_history = []
                self._frontdoor_prompt_cache_key_hash = ""
                self._frontdoor_actual_request_hash = ""
                self._frontdoor_actual_request_message_count = 0
                self._frontdoor_actual_tool_schema_hash = ""
                self._frontdoor_model_retry_status = None
                self._frontdoor_token_compression_applied_turn = False
            self._state.is_running = True
            self._state.paused = False
            self._state.status = "running"
            self._state.latest_message = ""
            self._reset_assistant_stream_state()
            self._state.last_error = None
            self._state.pending_tool_calls.clear()
            self._state.pending_interrupts = []
            self._last_verified_task_ids = []
            if persist_transcript:
                if internal_source is None:
                    await self._persist_pending_user_messages(
                        user_inputs=self._current_user_batch_inputs(user_input),
                    )
                else:
                    persisted_internal_prompt = await self._persist_internal_prompt_messages(
                        user_input=user_input,
                        internal_source=internal_source,
                    )
                    if internal_source == "cron" and persisted_internal_prompt is None:
                        raise RuntimeError("cron internal prompt persistence failed")

            await self._emit("agent_start", session_key=self._state.session_key, trigger="prompt")
            await self._emit("turn_start", session_key=self._state.session_key)
            await self._emit_state_snapshot()

            output = await self._run_message(user_input)
        except asyncio.CancelledError:
            terminal_stop_already_applied = (
                self._last_stop_reason == "user_pause"
                or str(self._state.status or "").strip().lower() in {"completed", "idle"}
            )
            if terminal_stop_already_applied:
                self._state.is_running = False
                raise
            already_paused = bool(self._state.paused) or str(self._state.status or "").strip().lower() == "paused"
            self._state.is_running = False
            self._state.paused = True
            self._state.status = "paused"
            if not already_paused:
                await self._emit("control_ack", action="pause", accepted=True)
            await self._emit("agent_end", session_key=self._state.session_key, status="paused")
            if not already_paused:
                await self._emit_state_snapshot()
            raise
        except CeoFrontdoorInterrupted as exc:
            return await self._pause_for_frontdoor_interrupt(exc)
        except Exception as exc:
            interaction_flow = self._interaction_flow_snapshot()
            user_text = self._history_text(user_input.content)
            recovered_dispatch = self._recover_dispatched_async_runtime_error(
                exc,
                interaction_flow=interaction_flow,
            )
            if recovered_dispatch is not None:
                output = str(recovered_dispatch.get("text") or "").strip()
                task_ids = self._normalize_verified_task_ids(recovered_dispatch.get("task_ids"))
                logger.opt(exception=exc).error(
                    "Recovered async dispatch turn after internal runtime error "
                    "(session_key={}, route_kind={}, internal_source={}, task_ids={})",
                    self._state.session_key,
                    str(getattr(self, "_last_route_kind", "") or ""),
                    internal_source or "user",
                    ",".join(task_ids),
                )
                # 与普通轮末收尾一致:不写指针摘要,该阶段的最终回复就是这个可见输出本身。
                self._frontdoor_stage_state = self._complete_active_frontdoor_stage_state(
                    self._frontdoor_stage_state,
                )
                assistant = AssistantMessage(content=output, timestamp=self._now())
                self._state.messages.append(assistant)
                self._cancel_assistant_stream_flush_task()
                self._assistant_stream_pending_text = ""
                self._state.latest_message = output
                self._state.is_running = False
                self._state.status = "completed"
                self._state.last_error = None
                self._state.pending_tool_calls.clear()
                self._last_verified_task_ids = list(task_ids)
                silent_reply = is_silent_reply_token(output)
                if getattr(self._loop, "prompt_trace", False):
                    logger.info(render_output_trace(output))
                if persist_transcript:
                    assistant_metadata = {
                        "task_ids": task_ids,
                        "reason": "async_dispatch_runtime_recovered",
                    }
                    if silent_reply:
                        assistant_metadata["silent_reply"] = True
                        assistant_metadata["prompt_visible"] = False
                    if cron_internal:
                        assistant_metadata["source"] = "cron"
                        assistant_metadata["cron_job_id"] = str(
                            (user_input.metadata or {}).get("cron_job_id") or ""
                        ).strip()
                    await self._persist_turn_transcript(
                        user_input=user_input,
                        user_text=user_text,
                        assistant_text="" if silent_reply else output,
                        interaction_flow=interaction_flow,
                        internal_source=internal_source,
                        route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                        assistant_metadata=assistant_metadata,
                    )
                await self._emit(
                    "message_end",
                    role="assistant",
                    text="" if silent_reply else output,
                    silent_reply=silent_reply,
                    heartbeat_internal=heartbeat_internal,
                    heartbeat_reason=str((user_input.metadata or {}).get("heartbeat_reason") or "").strip(),
                    source=internal_source or "user",
                    turn_id=self._current_turn_id(user_input),
                )
                if internal_source is None:
                    self.clear_paused_execution_context()
                await self._emit("turn_end", session_key=self._state.session_key, status="completed")
                await self._emit("agent_end", session_key=self._state.session_key, status="completed")
                await self._emit_state_snapshot()
                return RunResult(output="" if silent_reply else output, is_silent_reply=silent_reply, events=list(self._event_log))
            logger.opt(exception=exc).error(
                "Runtime agent turn failed "
                "(session_key={}, route_kind={}, internal_source={})",
                self._state.session_key,
                str(getattr(self, "_last_route_kind", "") or ""),
                internal_source or "user",
            )
            self._state.is_running = False
            self._state.status = "error"
            self._cancel_assistant_stream_flush_task()
            self._assistant_stream_pending_text = ""
            error_message = str(exc).strip()
            if not error_message:
                if isinstance(exc, MemoryError):
                    error_message = "运行时内存不足，未能完成当前轮次"
                else:
                    error_message = exc.__class__.__name__ or "unknown error"
            if isinstance(exc, StructuredError):
                error = exc
            elif all(hasattr(exc, key) for key in ("code", "message", "recoverable")):
                error_details: dict[str, Any] = {}
                exc_status = getattr(exc, "status", None)
                if exc_status is not None:
                    error_details["error_status"] = exc_status
                exc_kind = str(getattr(exc, "kind", "") or "").strip()
                if exc_kind:
                    error_details["error_kind"] = exc_kind
                error = StructuredError(
                    code=str(getattr(exc, "code", "") or "legacy_session_error"),
                    message=str(getattr(exc, "message", "") or error_message),
                    recoverable=bool(getattr(exc, "recoverable", True)),
                    details=error_details,
                )
            else:
                error = StructuredError(
                    code="legacy_session_error",
                    message=error_message,
                    recoverable=True,
                )
            self._state.last_error = error
            # Users see the complete error text so a failed turn explains itself
            # instead of collapsing into a bare "Error:" or a generic message.
            # The raw exception text also stays in the error file, the transcript
            # metadata and the "error" event for operators.
            if isinstance(exc, MemoryError):
                error_reply = "运行时内存不足，未能完成当前轮次"
            else:
                error_reply = self._turn_failed_reply(error_message)
            self._state.latest_message = error_reply
            self._persist_runtime_error_file(
                exc,
                user_text=user_text,
                interaction_flow=interaction_flow,
                internal_source=internal_source,
                route_kind=str(getattr(self, "_last_route_kind", "") or ""),
            )
            if persist_transcript:
                assistant_metadata = {
                    "source": "runtime_error",
                    "error_code": error.code,
                    "error_message": error.message,
                    "recoverable": error.recoverable,
                }
                # 透传结构化 provider 错误信号（HTTP 状态 / 异常类别），供运维与下游分支。
                for detail_key in ("error_status", "error_kind"):
                    detail_value = (error.details or {}).get(detail_key)
                    if detail_value not in (None, ""):
                        assistant_metadata[detail_key] = detail_value
                if cron_internal:
                    assistant_metadata["cron_job_id"] = str((user_input.metadata or {}).get("cron_job_id") or "").strip()
                persisted_session = await self._persist_turn_transcript(
                    user_input=user_input,
                    user_text=user_text,
                    assistant_text=error_reply,
                    interaction_flow=interaction_flow,
                    internal_source=internal_source,
                    route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                    assistant_metadata=assistant_metadata,
                )
                # 失败回合：回收本轮已落盘的内部提示词（心跳/cron 规则 system + 事件束
                # user），翻成 discarded 使其退出后续可重放上下文，避免反复堆积。助手错误
                # 行不带 internal_prompt_kind，不会被误伤；jsonl 原始行保留以备审计。
                if persisted_session is not None and internal_source is not None:
                    self._discard_internal_prompt_messages(
                        persisted_session,
                        self._current_turn_id(user_input),
                    )
                    self._loop.sessions.save(persisted_session)
            await self._emit(
                "error",
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
                source="runtime",
            )
            await self._emit("agent_end", session_key=self._state.session_key, status="error")
            await self._emit_state_snapshot()
            raise
        else:
            tail_profiler = _TurnTailProfiler(session_key=self._state.session_key)
            silent_reply = is_silent_reply_token(output)
            assistant = AssistantMessage(content="" if silent_reply else output, timestamp=self._now())
            self._state.messages.append(assistant)
            self._cancel_assistant_stream_flush_task()
            self._assistant_stream_pending_text = ""
            self._state.latest_message = "" if silent_reply else output
            self._state.is_running = False
            self._state.status = "completed"
            self._state.pending_tool_calls.clear()
            user_text = self._history_text(user_input.content)
            interaction_flow = self._interaction_flow_snapshot()
            if getattr(self._loop, "prompt_trace", False):
                logger.info(render_output_trace(output))
            persisted_session = None
            should_persist_transcript_reply = persist_transcript and not (
                internal_source is not None and str(output or "").strip() in {"", "HEARTBEAT_OK"}
            )
            if should_persist_transcript_reply:
                assistant_metadata = None
                if internal_source is not None:
                    assistant_metadata = {
                        "source": internal_source,
                        "prompt_visible": True,
                        "ui_visible": True,
                    }
                    if cron_internal:
                        assistant_metadata["cron_job_id"] = str((user_input.metadata or {}).get("cron_job_id") or "").strip()
                if silent_reply:
                    # 静默回合落一条空文本 assistant 行：回复本身被吞掉，但这行携带本轮的
                    # canonical_context，让 Web 会话框刷新后仍能渲染阶段轨道与工具步骤。
                    # prompt_visible=False 与 `_graph_finalize_turn` 一致——静默输出既不回填
                    # 请求体基线，也不该经转录重放回到模型上下文。
                    assistant_metadata = {
                        **(assistant_metadata or {}),
                        "prompt_visible": False,
                        "ui_visible": True,
                        "silent_reply": True,
                    }
                persisted_session = await self._persist_turn_transcript(
                    user_input=user_input,
                    user_text=user_text,
                    assistant_text="" if silent_reply else output,
                    interaction_flow=interaction_flow,
                    internal_source=internal_source,
                    route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                    assistant_metadata=assistant_metadata,
                    retire_lingering_transcript_rows=True,
                )
                tail_profiler.mark("persist_transcript")
                if not silent_reply and getattr(self._loop, "memory_manager", None) is not None:
                    # Memory review mirrors the user-visible surface: internal
                    # heartbeat/cron turns only expose the assistant reply and the
                    # visible stage rail, never the hidden event-bundle prompt.
                    if internal_source is None:
                        user_messages = [
                            self._history_text(item.content)
                            for item in self._current_user_batch_inputs(user_input)
                            if self._history_text(item.content).strip()
                        ]
                    else:
                        user_messages = []
                    try:
                        await self._loop.memory_manager.record_turn_for_review(
                            session_key=self._state.session_key,
                            turn_id=self._current_turn_id(user_input),
                            user_messages=user_messages,
                            assistant_text=output,
                            compression_summary=self._compression_snapshot(),
                            canonical_summary=self._frontdoor_visible_canonical_context_snapshot(),
                        )
                    except Exception:
                        await self._emit(
                            "message_delta",
                            channel="analysis",
                            kind="persistence_warning",
                            text="Memory review enqueue failed; turn history is still available in session transcript.",
                        )
                    tail_profiler.mark("record_turn_for_review")
                if getattr(self._loop, "memory_manager", None) is not None:
                    # 只在本轮真实发生内联 token 压缩时冲刷复核窗口。
                    # _frontdoor_history_shrink_reason 是“baseline 为何比上一轮短”的
                    # 粘滞解释，会跨轮残留，不能当作本轮压缩事件信号。
                    if bool(getattr(self, "_frontdoor_token_compression_applied_turn", False)):
                        await self._flush_memory_review_after_compression()
                        tail_profiler.mark("memory_review_flush")
            await self._emit(
                "message_end",
                role="assistant",
                text="" if silent_reply else output,
                silent_reply=silent_reply,
                heartbeat_internal=heartbeat_internal,
                heartbeat_reason=str((user_input.metadata or {}).get("heartbeat_reason") or "").strip(),
                source=internal_source or "user",
                turn_id=self._current_turn_id(user_input),
            )
            tail_profiler.mark("emit_message_end")
            if internal_source is None:
                self.clear_paused_execution_context()
            await self._emit("turn_end", session_key=self._state.session_key, status="completed")
            await self._emit("agent_end", session_key=self._state.session_key, status="completed")
            await self._emit_state_snapshot()
            tail_profiler.mark("emit_terminal_events")
            tail_profiler.warn_if_slow()
            return RunResult(output="" if silent_reply else output, is_silent_reply=silent_reply, events=list(self._event_log))
        finally:
            if self._active_cancel_token is cancel_token:
                self._active_cancel_token = None
            self._active_turn_id = None
            self._clear_user_batch_context()
            self._loop.release_session_cancellation_token(self._state.session_key, cancel_token)

    async def prompt(
        self,
        message: str | UserInputMessage,
        *,
        persist_transcript: bool = True,
        live_context: dict[str, str] | None = None,
    ) -> RunResult:
        async with self._turn_lock:
            user_input = message if isinstance(message, UserInputMessage) else UserInputMessage(content=str(message))
            if self._internal_prompt_source(user_input) is None:
                self._configure_user_batch([user_input])
            return await self._prompt_locked(
                user_input,
                persist_transcript=persist_transcript,
                live_context=live_context,
            )

    def _normalize_user_batch_inputs(
        self,
        messages: list[str | UserInputMessage],
    ) -> list[UserInputMessage]:
        normalized_inputs: list[UserInputMessage] = []
        for raw in list(messages or []):
            item = raw if isinstance(raw, UserInputMessage) else UserInputMessage(content=str(raw))
            if self._internal_prompt_source(item) is not None:
                raise ValueError("prompt_batch_user_messages_only")
            text = self._history_text(item.content)
            if not text.strip() and not item.attachments:
                continue
            normalized_inputs.append(item)
        return normalized_inputs

    def _assign_transcript_batch_without_activating(
        self,
        user_inputs: list[UserInputMessage],
        *,
        batch_id: str | None = None,
    ) -> list[UserInputMessage]:
        resolved_batch_id = str(batch_id or "").strip() or self._new_batch_id()
        prepared: list[UserInputMessage] = []
        for item in list(user_inputs or []):
            if not isinstance(item, UserInputMessage):
                continue
            metadata = dict(item.metadata or {})
            turn_id = str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or "").strip() or self._new_turn_id()
            metadata[_TRANSCRIPT_TURN_ID_KEY] = turn_id
            metadata[_TRANSCRIPT_BATCH_ID_KEY] = resolved_batch_id
            item.metadata = metadata
            prepared.append(item)
        return prepared

    @staticmethod
    def _user_input_turn_id(user_input: UserInputMessage | None) -> str:
        if not isinstance(user_input, UserInputMessage):
            return ""
        metadata = dict(user_input.metadata or {})
        return str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or "").strip()

    async def prompt_batch(
        self,
        messages: list[str | UserInputMessage],
        *,
        persist_transcript: bool = True,
        live_context: dict[str, str] | None = None,
    ) -> RunResult:
        normalized_inputs = self._normalize_user_batch_inputs(messages)
        if not normalized_inputs:
            raise ValueError("prompt_batch_requires_messages")
        combined_query_text = self._batch_query_text(normalized_inputs)
        batch_id = self._new_batch_id()
        self._configure_user_batch(normalized_inputs, batch_id=batch_id)
        last_input = normalized_inputs[-1]
        last_metadata = dict(last_input.metadata or {})
        if combined_query_text:
            last_metadata["web_ceo_batch_query_text"] = combined_query_text
            last_input.metadata = last_metadata
        async with self._turn_lock:
            return await self._prompt_locked(
                last_input,
                persist_transcript=persist_transcript,
                live_context=live_context,
            )

    async def queue_follow_up_batch(
        self,
        messages: list[str | UserInputMessage],
        *,
        persist_transcript: bool = True,
    ) -> list[UserInputMessage]:
        normalized_inputs = self._normalize_user_batch_inputs(messages)
        if not normalized_inputs:
            return []
        self._capture_follow_up_transition_snapshot()
        queued_inputs = self._assign_transcript_batch_without_activating(normalized_inputs)
        self._state.queued_follow_up_messages.extend(queued_inputs)
        if persist_transcript:
            await self._persist_pending_user_messages(user_inputs=queued_inputs)
        return list(queued_inputs)

    async def take_follow_up_batch_for_call_model(self) -> list[UserInputMessage]:
        queued_inputs = [
            item
            for item in list(self._state.queued_follow_up_messages or [])
            if isinstance(item, UserInputMessage)
        ]
        follow_up_turn_ids = {
            self._user_input_turn_id(item)
            for item in queued_inputs
            if self._user_input_turn_id(item)
        }
        self._state.queued_follow_up_messages.clear()
        if not queued_inputs:
            return []
        await self._archive_inflight_assistant_for_follow_up_ui_history(
            pending_follow_up_turn_ids=follow_up_turn_ids,
        )
        self._follow_up_transition_snapshot = None
        drained_inputs = self._assign_transcript_batch_without_activating(queued_inputs)
        existing_inputs = [
            item
            for item in list(self._active_user_batch_inputs or [])
            if isinstance(item, UserInputMessage)
        ]
        seen_turn_ids = {
            self._user_input_turn_id(item)
            for item in existing_inputs
            if self._user_input_turn_id(item)
        }
        for item in drained_inputs:
            turn_id = self._user_input_turn_id(item)
            if turn_id and turn_id in seen_turn_ids:
                continue
            existing_inputs.append(item)
            if turn_id:
                seen_turn_ids.add(turn_id)
        self._active_user_batch_inputs = list(existing_inputs)
        await self._persist_pending_user_messages(user_inputs=drained_inputs)
        return list(drained_inputs)

    def drain_queued_follow_up_messages(self) -> list[UserInputMessage]:
        queued_inputs = [
            item
            for item in list(self._state.queued_follow_up_messages or [])
            if isinstance(item, UserInputMessage)
        ]
        self._state.queued_follow_up_messages.clear()
        return queued_inputs

    def _rehydrate_queued_follow_ups(self) -> int:
        """把转录里仍是 pending 的用户条目接回内存队列，返回接回的条数。

        队列的 durable 那一半本来就在盘上：`queue_follow_up_batch` 入队时写一条
        `_transcript_state=pending` 的用户行，回合真跑起来后 `_persist_turn_transcript`
        按同一个 turn_id 把它升成 completed。缺的只有读它的人——此前
        `_TRANSCRIPT_STATE_PENDING` 全仓零读者，所以重启后队列归零，那条消息停在转录里
        显示成"已发送"，却再也不会被回答。

        已经有助手行的 turn 不接回：那一轮已经答过，只是崩在状态翻转之前，重发就是重复回答。"""
        session_key = str(self._state.session_key or "").strip()
        if not session_key or getattr(self._loop, "sessions", None) is None:
            return 0
        if list(self._state.queued_follow_up_messages or []):
            return 0
        try:
            rows = list(self._loop.sessions.get_or_create(session_key).messages or [])
        except Exception:
            logger.debug("queued follow-up rehydrate skipped for {}", session_key)
            return 0
        answered_turn_ids = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get("role") or "") != "assistant":
                continue
            turn_id = str(
                row.get("turn_id")
                or ((row.get("metadata") or {}).get(_TRANSCRIPT_TURN_ID_KEY))
                or ""
            ).strip()
            if turn_id:
                answered_turn_ids.add(turn_id)
        restored: list[UserInputMessage] = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("role") or "") != "user":
                continue
            metadata = dict(row.get("metadata") or {})
            if str(metadata.get(_TRANSCRIPT_STATE_KEY) or "").strip().lower() != _TRANSCRIPT_STATE_PENDING:
                continue
            turn_id = str(metadata.get(_TRANSCRIPT_TURN_ID_KEY) or "").strip()
            if turn_id and turn_id in answered_turn_ids:
                continue
            content = str(row.get("content") or "")
            attachments = list(row.get("attachments") or [])
            if not content.strip() and not attachments:
                continue
            restored.append(
                UserInputMessage(
                    content=content,
                    attachments=attachments,
                    metadata=metadata,
                    # 接回的是 durable 行：送达时间沿用行上的原始时间。丢掉它会让 inflight
                    # 快照把很久以前排队的消息报成刚发送，前端也就无法按时间落位气泡。
                    timestamp=str(row.get("timestamp") or "").strip() or None,
                )
            )
        if restored:
            self._state.queued_follow_up_messages.extend(restored)
            # 重启后"突然回答一条旧消息"必须有迹可循：运营看到的是这一行，而不是
            # 转录里一条一直停在 pending 的静默消息。
            logger.info(
                "Rehydrated {} queued follow-up message(s) for {}",
                len(restored),
                session_key,
            )
        return len(restored)

    async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict[str, Any]:
        """会话回到空闲时把排队中的消息发出去。返回 {dispatched, reason}。

        排水通道本来只有两条，且都长在请求处理里：WS 的回合链
        （api/websocket_ceo `_run_user_turn`）和 external 的 `_drain_queued_follow_ups`。
        于是"没有客户端在等回包"的场合——手动压缩刚结束、浏览器刷新过、进程重启过——
        排队的消息会一直躺在队列里。这里补第三条通道：谁让会话变空闲，谁在自己的收尾调用它。

        出站沿用渠道自己那条路：`make_session_event_relay` 只要 session_key 和 turn_id
        就能把 reply.final 发到该会话的 hub，QQ 桥的 SSE pump 照收（cron 就是这个形状）。
        web 会话不需要额外投递：WS 的订阅是 socket 级而非按消息的，转录与会话摘要负责重连。

        失败时把条目放回队列（转录那行仍是 pending，重启后也会被重新接回），因为这三个
        缝都是低频事件，不会形成重试热循环。"""
        session_key = str(self._state.session_key or "").strip()
        hold = self.frontdoor_inbound_hold()
        if hold:
            return {"dispatched": 0, "reason": f"held:{hold}", "source": source}
        if list(self._state.pending_interrupts or []):
            return {"dispatched": 0, "reason": "tool_approval_pending", "source": source}
        queued = self.drain_queued_follow_up_messages()
        if not queued:
            return {"dispatched": 0, "reason": "empty", "source": source}

        turn_id = uuid.uuid4().hex
        unsubscribe: Callable[[], Any] | None = None
        hub: Any = None
        try:
            from g3ku.runtime.external_events import get_session_event_hub, make_session_event_relay

            relay = make_session_event_relay(session_key, turn_id=turn_id, session=self)
            unsubscribe = self.subscribe(relay)
            hub = get_session_event_hub(session_key)
            hub.publish("turn.started", turn_id=turn_id)
        except Exception:
            logger.debug("queued follow-up relay unavailable for {}", session_key)

        try:
            try:
                result = await self.prompt_batch(queued)
            finally:
                # 订阅必须随这次派发结束：留在 self._listeners 里会让之后每一个回合的
                # 事件继续往这个已终局的 turn_id 上灌。
                if callable(unsubscribe):
                    try:
                        unsubscribe()
                    except Exception:
                        logger.debug("queued follow-up relay unsubscribe failed for {}", session_key)
        except Exception as exc:
            self._restore_queued_follow_ups_front(queued)
            if hub is not None:
                self._publish_queued_follow_up_terminal(hub, turn_id, failed=True)
            logger.warning(
                "Queued follow-up dispatch failed for {} ({} message(s) restored): {}",
                session_key,
                len(queued),
                exc,
            )
            return {"dispatched": 0, "reason": "dispatch_failed", "source": source}
        if hub is not None:
            self._publish_queued_follow_up_terminal(hub, turn_id, failed=False)
        logger.info(
            "Dispatched {} queued follow-up message(s) for {} ({})",
            len(queued),
            session_key,
            source or "idle",
        )
        return {
            "dispatched": len(queued),
            "reason": "",
            "source": source,
            "turn_id": turn_id,
            "output": str(getattr(result, "output", "") or ""),
        }

    def _restore_queued_follow_ups_front(self, queued: list[UserInputMessage]) -> None:
        restored = [
            item
            for item in list(queued or [])
            if isinstance(item, UserInputMessage)
        ]
        if not restored:
            return
        remaining = list(self._state.queued_follow_up_messages or [])
        self._state.queued_follow_up_messages[:] = [*restored, *remaining]

    @staticmethod
    def _publish_queued_follow_up_terminal(hub: Any, turn_id: str, *, failed: bool) -> None:
        """渠道契约：每个回合恰好一个终局事件，派发出去的回合也不能只发 started。"""
        try:
            hub.publish("turn.failed" if failed else "turn.completed", turn_id=turn_id)
        except Exception:
            return

    async def continue_(self, *, live_context: dict[str, str] | None = None) -> RunResult:
        return await self.prompt(self._last_prompt, live_context=live_context)

    def steer(self, message: str | UserInputMessage) -> None:
        content = message.content if isinstance(message, UserInputMessage) else str(message)
        self._state.queued_steering_messages.append(UserInputMessage(content=content))

    def follow_up(self, message: str | UserInputMessage) -> None:
        content = message.content if isinstance(message, UserInputMessage) else str(message)
        self._state.queued_follow_up_messages.append(UserInputMessage(content=content))

    async def pause(self, *, manual: bool = False) -> None:
        self._frontdoor_model_retry_status = None
        if self._background_tool_targets:
            manager = getattr(self._loop, "tool_execution_manager", None)
            if manager is not None and hasattr(manager, "stop_execution"):
                for execution_id in list(self._background_tool_targets.keys()):
                    try:
                        await manager.stop_execution(
                            execution_id,
                            reason="session_pause_requested",
                        )
                    except Exception:
                        logger.debug("background tool stop skipped for {}", execution_id)
        self._state.paused = True
        self._state.is_running = False
        self._state.status = "paused"
        paused_snapshot = (
            self._build_execution_context_snapshot(allow_manual_pause=True, status_override="paused")
            if manual
            else None
        )
        if manual:
            self._set_paused_execution_context(paused_snapshot)
        await self._emit_safe_stop_notice("pause")
        # 暂停正好落在自动压缩途中：区分线要停在「压缩已暂停」而不是无声消失，否则会
        # 留下一段既没有摘要也没有标记的历史。手动压缩走自己的收尾，不重复落线。
        pausing_active_compression = (
            manual and getattr(self, "_active_frontdoor_compression_generation", None) is not None
        )
        self._cancel_active_frontdoor_compression_generation()
        if self._active_cancel_token is not None:
            self._active_cancel_token.cancel(reason="用户已请求暂停，正在安全停止...")
        await self._loop.cancel_session_tasks(self._state.session_key)
        self._state.pending_tool_calls.clear()
        self._pending_tool_call_names.clear()
        self._pending_tool_name_calls.clear()
        self._background_tool_targets.clear()
        self._state.pending_interrupts = []
        self._preserved_inflight_turn = None
        if manual:
            await self._persist_manual_pause_user_messages()
            await self._archive_paused_execution_context_for_ui_history()
            self._state.paused = False
            self._state.status = "completed"
            self._last_stop_reason = "user_pause"
            self._sync_completed_continuity_snapshot(source_reason="manual_stop")
            self.clear_paused_execution_context()
            if pausing_active_compression:
                self.append_context_compression_marker(state="paused", source="auto")
                # 摘要任务可能在 cancel_session_tasks 里被直接掐掉，来不及走自己的
                # finally；这里替它收口，否则下一回合会被一个已无人认领的代际挡住。
                self._compression_state = {}
                self._active_frontdoor_compression_generation = None
            # Manual pause persists the current prompt's transcript state using the
            # existing turn id so the pending user message can be updated in place.
            # Clear the active turn binding again afterwards so the next real user
            # message starts a fresh transcript turn instead of overwriting the
            # paused request that was just preserved.
            self._active_turn_id = None
        else:
            self._last_stop_reason = ""
        await self._emit(
            "control_ack",
            action="pause",
            accepted=True,
            source=self._internal_prompt_source() or "user",
        )
        await self._emit_state_snapshot()

    async def resume(self, *, replan: bool = False, additional_context: str | None = None) -> RunResult:
        self._set_manual_pause_waiting_reason(False)
        if additional_context:
            await self._emit("control_ack", action="resume", accepted=True, replan=replan)
            await self._emit_state_snapshot()
            return await self.prompt_batch([UserInputMessage(content=str(additional_context or "").strip())])
        self._state.paused = False
        self._state.status = "running"
        await self._emit("control_ack", action="resume", accepted=True, replan=replan)
        await self._emit_state_snapshot()
        return RunResult(output="", events=list(self._event_log))

    async def resume_frontdoor_interrupt(
        self,
        *,
        resume_value: Any,
        live_context: dict[str, str] | None = None,
    ) -> RunResult:
        from g3ku.shells.web import refresh_web_agent_runtime

        async with self._turn_lock:
            self._apply_live_context(live_context)
            await refresh_web_agent_runtime(force=False, reason="resume_interrupt")
            runner = getattr(self._loop, "multi_agent_runner", None)
            if runner is None or not hasattr(runner, "resume_turn"):
                raise RuntimeError("frontdoor_interrupt_resume_unavailable")
            paused_snapshot = self.paused_execution_context_snapshot()
            if isinstance(paused_snapshot, dict):
                paused_turn_id = str(paused_snapshot.get("turn_id") or "").strip()
                if paused_turn_id and not str(self._active_turn_id or "").strip():
                    self._active_turn_id = paused_turn_id
            self._event_log = []
            self._state.is_running = True
            self._state.paused = False
            self._state.status = "running"
            self._frontdoor_model_retry_status = None
            self._state.latest_message = ""
            self._reset_assistant_stream_state()
            self._state.last_error = None
            self._state.pending_tool_calls.clear()
            self._pending_tool_call_names.clear()
            self._pending_tool_name_calls.clear()
            self._background_tool_targets.clear()
            self._state.pending_interrupts = []
            await self._emit("control_ack", action="resume_interrupt", accepted=True)
            await self._emit_state_snapshot()
            try:
                output = await runner.resume_turn(
                    session=self,
                    resume_value=resume_value,
                    on_progress=self._handle_progress,
                )
            except CeoFrontdoorInterrupted as exc:
                return await self._pause_for_frontdoor_interrupt(exc)
            self.clear_paused_execution_context()
            self._state.is_running = False
            self._state.paused = False
            self._state.status = "completed"
            self._cancel_assistant_stream_flush_task()
            self._assistant_stream_pending_text = ""
            silent_reply = is_silent_reply_token(output)
            self._state.latest_message = "" if silent_reply else str(output or "")
            await self._emit(
                "message_end",
                role="assistant",
                text="" if silent_reply else str(output or ""),
                silent_reply=silent_reply,
                source="user",
                turn_id=self._current_turn_id(),
            )
            await self._emit_state_snapshot()
            return RunResult(output="" if silent_reply else str(output or ""), is_silent_reply=silent_reply, events=list(self._event_log))

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        await self._emit_safe_stop_notice("cancel")
        self._cancel_active_frontdoor_compression_generation()
        if self._active_cancel_token is not None:
            self._active_cancel_token.cancel(reason=reason or "用户已请求停止，正在安全停止...")
        await self._loop.cancel_session_tasks(self._state.session_key)
        self._set_manual_pause_waiting_reason(False)
        self._preserved_inflight_turn = None
        self.clear_paused_execution_context()
        self._last_stop_reason = ""
        self._state.is_running = False
        self._state.paused = False
        self._state.status = "idle"
        self._state.pending_tool_calls.clear()
        self._pending_tool_call_names.clear()
        self._pending_tool_name_calls.clear()
        self._background_tool_targets.clear()
        self._state.pending_interrupts = []
        await self._emit("control_ack", action="cancel", accepted=True, reason=reason)
        await self._emit_state_snapshot()

    async def _emit_safe_stop_notice(self, action: str) -> None:
        message = "用户已请求暂停，正在安全停止..." if action == "pause" else "用户已请求停止，正在安全停止..."
        if self._pending_tool_call_names:
            for call_id, tool_name in list(self._pending_tool_call_names.items()):
                await self._handle_progress(
                    message,
                    event_kind="tool",
                    event_data={"tool_name": tool_name, "tool_call_id": call_id},
                )
            return
        await self._emit(
            "message_delta",
            channel="progress",
            kind="progress",
            text=message,
            data={"action": action},
        )

    def set_model(self, model: str) -> None:
        self._state.model = model

    def set_reasoning_effort(self, level: str | None) -> None:
        self._state.reasoning_effort = level

    @staticmethod
    def _event_tool_call_id(data: dict[str, Any] | None) -> str:
        return str((data or {}).get("tool_call_id") or "").strip()

    @staticmethod
    def _normalize_tool_name(tool_name: str) -> str:
        return str(tool_name or "tool").strip() or "tool"

    def _peek_pending_tool_call_id(self, tool_name: str) -> str:
        normalized = self._normalize_tool_name(tool_name)
        pending = self._pending_tool_name_calls.get(normalized)
        while pending:
            call_id = str(pending[0] or "").strip()
            if call_id and self._pending_tool_call_names.get(call_id) == normalized:
                return call_id
            pending.popleft()
        if pending is not None and not pending:
            self._pending_tool_name_calls.pop(normalized, None)
        return ""

    def _discard_pending_tool_call(self, tool_call_id: str) -> None:
        call_id = str(tool_call_id or "").strip()
        if not call_id:
            return
        tool_name = self._pending_tool_call_names.pop(call_id, "")
        if not tool_name:
            return
        pending = self._pending_tool_name_calls.get(tool_name)
        if pending is None:
            return
        filtered = deque(item for item in pending if str(item or "").strip() != call_id)
        if filtered:
            self._pending_tool_name_calls[tool_name] = filtered
        else:
            self._pending_tool_name_calls.pop(tool_name, None)
