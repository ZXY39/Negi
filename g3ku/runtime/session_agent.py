from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.prompt_trace import render_output_trace
from g3ku.core.events import AgentEvent
from g3ku.core.messages import AssistantMessage, UserInputMessage
from g3ku.core.results import RunResult
from g3ku.core.state import AgentState, StructuredError
from g3ku.runtime.frontdoor.canonical_context import (
    canonical_context_tool_items,
    default_frontdoor_canonical_context,
    normalize_frontdoor_canonical_context,
)
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.semantic_context_summary import default_semantic_context_state

_CONTROL_TOOL_NAMES = {"stop_tool_execution"}
_LEGACY_CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}
_TRANSCRIPT_TURN_ID_KEY = "_transcript_turn_id"
_TRANSCRIPT_BATCH_ID_KEY = "_transcript_batch_id"
_TRANSCRIPT_STATE_KEY = "_transcript_state"
_TRANSCRIPT_STATE_PENDING = "pending"
_TRANSCRIPT_STATE_PAUSED = "paused"
_TRANSCRIPT_STATE_COMPLETED = "completed"
_TASK_ID_PATTERN = re.compile(r"task:[A-Za-z0-9][\w:-]*")


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
        self._preserved_inflight_turn: dict[str, Any] | None = None
        self._paused_execution_context: dict[str, Any] | None = None
        self._frontdoor_stage_state: dict[str, Any] = {}
        self._frontdoor_canonical_context: dict[str, Any] = default_frontdoor_canonical_context()
        self._compression_state: dict[str, Any] = {}
        self._semantic_context_state: dict[str, Any] = default_semantic_context_state()
        self._frontdoor_hydrated_tool_names: list[str] = []
        self._frontdoor_selection_debug: dict[str, Any] = {}
        self._frontdoor_actual_request_path: str = ""
        self._frontdoor_actual_request_history: list[dict[str, Any]] = []
        self._frontdoor_prompt_cache_key_hash: str = ""
        self._frontdoor_actual_request_hash: str = ""
        self._frontdoor_actual_request_message_count: int = 0
        self._frontdoor_actual_tool_schema_hash: str = ""
        self._active_turn_id: str | None = None
        self._active_batch_id: str | None = None
        self._active_user_batch_inputs: list[UserInputMessage] = []
        self._last_verified_task_ids: list[str] = []
        self._turn_lock = asyncio.Lock()

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
        return data

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
        return {
            "text": (
                f"后台任务已经建立，任务号 `{primary_task_id}`。"
                "当前回写遇到暂时异常，但后台任务仍在运行，完成后会继续同步结果。"
            ),
            "task_ids": task_ids,
        }

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
        if canonical_context:
            assistant_payload["canonical_context"] = canonical_context
        if compression:
            assistant_payload["compression"] = compression
        try:
            persisted_session = self._loop.sessions.get_or_create(self._state.session_key)
            existing_index = self._find_archived_paused_assistant_index(persisted_session, turn_id=turn_id)
            if existing_index is None:
                persisted_session.add_message("assistant", assistant_text, **assistant_payload)
            else:
                archived_message = dict(persisted_session.messages[existing_index])
                archived_message["content"] = assistant_text
                archived_message["turn_id"] = turn_id
                archived_message["status"] = "paused"
                archived_message["metadata"] = metadata
                if canonical_context:
                    archived_message["canonical_context"] = canonical_context
                else:
                    archived_message.pop("canonical_context", None)
                if compression:
                    archived_message["compression"] = compression
                else:
                    archived_message.pop("compression", None)
                persisted_session.messages[existing_index] = archived_message
                if hasattr(persisted_session, "updated_at"):
                    persisted_session.updated_at = datetime.now()
            if self._state.session_key.startswith("web:"):
                from g3ku.runtime.web_ceo_sessions import build_last_task_memory, ensure_ceo_session_metadata

                changed = ensure_ceo_session_metadata(persisted_session)
                metadata_payload = dict(getattr(persisted_session, "metadata", {}) or {})
                next_task_memory = build_last_task_memory(persisted_session)
                if metadata_payload.get("last_task_memory") != next_task_memory:
                    metadata_payload["last_task_memory"] = next_task_memory
                    changed = True
                if changed:
                    persisted_session.metadata = metadata_payload
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
        prompt = self._last_prompt
        attachments: list[dict[str, Any]] = []
        timestamp: str | None = None
        if isinstance(prompt, UserInputMessage):
            if self._internal_prompt_source(prompt) is not None:
                return None
            metadata = dict(prompt.metadata or {})
            raw_text = metadata.get("web_ceo_raw_text")
            text = str(raw_text) if isinstance(raw_text, str) else self._history_text(prompt.content)
            attachments = self._normalize_web_uploads(metadata.get("web_ceo_uploads"))
            timestamp = prompt.timestamp
        else:
            text = self._history_text(prompt)
        if not text.strip() and not attachments:
            return None
        payload: dict[str, Any] = {"role": "user", "content": text}
        if attachments:
            payload["attachments"] = attachments
        if isinstance(timestamp, str) and timestamp.strip():
            payload["timestamp"] = timestamp.strip()
        return payload

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

    @staticmethod
    def _semantic_context_state_has_material_content(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if str(value.get("summary_text") or "").strip():
            return True
        if bool(value.get("needs_refresh")):
            return True
        if str(value.get("updated_at") or "").strip():
            return True
        if str(value.get("coverage_history_source") or "").strip():
            return True
        try:
            coverage_message_index = int(value.get("coverage_message_index", -1) or -1)
        except (TypeError, ValueError):
            coverage_message_index = -1
        if coverage_message_index >= 0:
            return True
        try:
            coverage_stage_index = int(value.get("coverage_stage_index", 0) or 0)
        except (TypeError, ValueError):
            coverage_stage_index = 0
        if coverage_stage_index > 0:
            return True
        if str(value.get("failure_cooldown_until") or "").strip():
            return True
        return False

    def _semantic_context_state_snapshot(self) -> dict[str, Any]:
        raw = getattr(self, "_semantic_context_state", None)
        if not isinstance(raw, dict):
            return {}
        snapshot = copy.deepcopy(raw)
        if not self._semantic_context_state_has_material_content(snapshot):
            return {}
        return snapshot

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
            "semantic_context_state": copy.deepcopy(getattr(self, "_semantic_context_state", None) or {}),
            "hydrated_tool_names": [
                str(item or "").strip()
                for item in list(getattr(self, "_frontdoor_hydrated_tool_names", []) or [])
                if str(item or "").strip()
            ],
            "frontdoor_selection_debug": copy.deepcopy(getattr(self, "_frontdoor_selection_debug", None) or {}),
        }
        return snapshot

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
        if not (self._state.is_running or status in {"running", "paused", "error"}):
            return None
        canonical_context = self._frontdoor_visible_canonical_context_snapshot()
        frontdoor_canonical_context = self._frontdoor_canonical_context_snapshot()
        compression = self._compression_snapshot()
        semantic_context_state = self._semantic_context_state_snapshot()
        frontdoor_stage_state = (
            copy.deepcopy(getattr(self, "_frontdoor_stage_state", None) or {})
            if self._has_renderable_frontdoor_stage_state()
            else {}
        )
        snapshot: dict[str, Any] = {
            "status": status or ("running" if self._state.is_running else "idle"),
            "compression": compression,
        }
        if canonical_context:
            snapshot["canonical_context"] = canonical_context
        if frontdoor_stage_state:
            snapshot["frontdoor_stage_state"] = frontdoor_stage_state
        if frontdoor_canonical_context:
            snapshot["frontdoor_canonical_context"] = frontdoor_canonical_context
        if semantic_context_state:
            snapshot["semantic_context_state"] = semantic_context_state
        turn_id = self._current_turn_id()
        if turn_id:
            snapshot["turn_id"] = turn_id
        prompt = self._last_prompt
        prompt_source = self._internal_prompt_source(prompt)
        if prompt_source is not None:
            snapshot["source"] = prompt_source
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
            and "semantic_context_state" not in snapshot
            and "hydrated_tool_names" not in snapshot
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
            assistant_payload: dict[str, Any] = {}
            canonical_context = self._frontdoor_visible_canonical_context_snapshot()
            compression = self._compression_snapshot()
            if canonical_context:
                assistant_payload["canonical_context"] = canonical_context
            if compression:
                assistant_payload["compression"] = compression
            metadata_payload = dict(assistant_metadata or {})
            if internal_source is not None:
                metadata_payload.setdefault("source", internal_source)
                metadata_payload["history_visible"] = False
            verified_task_ids = self._normalize_verified_task_ids(self._last_verified_task_ids)
            if verified_task_ids:
                metadata_payload["task_ids"] = verified_task_ids
            if metadata_payload:
                assistant_payload["metadata"] = metadata_payload
            persisted_session.add_message("assistant", assistant_text, **assistant_payload)
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

        if kind == "tool_start":
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
            text = str(content or "").strip()
            if text and self._state.latest_message != text:
                self._state.latest_message = text
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
        semantic_context_state = interrupt_values.get("semantic_context_state")
        hydrated_tool_names = interrupt_values.get("hydrated_tool_names")
        frontdoor_selection_debug = interrupt_values.get("frontdoor_selection_debug")
        preserved_frontdoor_stage_state = getattr(self, "_frontdoor_stage_state", None)
        preserved_frontdoor_canonical_context = getattr(self, "_frontdoor_canonical_context", None)
        preserved_compression_state = getattr(self, "_compression_state", None)
        preserved_semantic_context_state = getattr(self, "_semantic_context_state", None)
        preserved_hydrated_tool_names = getattr(self, "_frontdoor_hydrated_tool_names", None)
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
        self._semantic_context_state = (
            dict(semantic_context_state)
            if isinstance(semantic_context_state, dict)
            else dict(preserved_semantic_context_state)
            if isinstance(preserved_semantic_context_state, dict)
            else default_semantic_context_state()
        )
        self._frontdoor_hydrated_tool_names = [
            str(item or "").strip()
            for item in list(hydrated_tool_names or preserved_hydrated_tool_names or [])
            if str(item or "").strip()
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
                "graph_state": interrupt_values,
            }
        )
        await self._emit("frontdoor_interrupt", interrupts=serialized_interrupts)
        await self._emit_state_snapshot()
        return RunResult(output="", events=list(self._event_log))

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
            self._pending_tool_call_names.clear()
            self._pending_tool_name_calls.clear()
            self._background_tool_targets.clear()
            if reset_frontdoor_turn_state:
                # Fresh visible turns and internal heartbeat/cron turns each start from
                # their own frontdoor runtime window.
                self._frontdoor_stage_state = {}
                self._compression_state = {}
                self._frontdoor_selection_debug = {}
                self._frontdoor_actual_request_path = ""
                self._frontdoor_actual_request_history = []
                self._frontdoor_prompt_cache_key_hash = ""
                self._frontdoor_actual_request_hash = ""
                self._frontdoor_actual_request_message_count = 0
                self._frontdoor_actual_tool_schema_hash = ""
            self._state.is_running = True
            self._state.paused = False
            self._state.status = "running"
            self._state.latest_message = ""
            self._state.last_error = None
            self._state.pending_tool_calls.clear()
            self._state.pending_interrupts = []
            self._last_verified_task_ids = []
            if persist_transcript and internal_source is None:
                await self._persist_pending_user_messages(
                    user_inputs=self._current_user_batch_inputs(user_input),
                )

            await self._emit("agent_start", session_key=self._state.session_key, trigger="prompt")
            await self._emit("turn_start", session_key=self._state.session_key)
            await self._emit_state_snapshot()

            output = await self._run_message(user_input)
        except asyncio.CancelledError:
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
                self._frontdoor_stage_state = self._complete_active_frontdoor_stage_state(
                    self._frontdoor_stage_state,
                    completed_stage_summary=output,
                )
                assistant = AssistantMessage(content=output, timestamp=self._now())
                self._state.messages.append(assistant)
                self._state.latest_message = output
                self._state.is_running = False
                self._state.status = "completed"
                self._state.last_error = None
                self._state.pending_tool_calls.clear()
                self._last_verified_task_ids = list(task_ids)
                if getattr(self._loop, "prompt_trace", False):
                    logger.info(render_output_trace(output))
                if persist_transcript:
                    assistant_metadata = {
                        "task_ids": task_ids,
                        "reason": "async_dispatch_runtime_recovered",
                    }
                    if cron_internal:
                        assistant_metadata["source"] = "cron"
                        assistant_metadata["cron_job_id"] = str(
                            (user_input.metadata or {}).get("cron_job_id") or ""
                        ).strip()
                    await self._persist_turn_transcript(
                        user_input=user_input,
                        user_text=user_text,
                        assistant_text=output,
                        interaction_flow=interaction_flow,
                        internal_source=internal_source,
                        route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                        assistant_metadata=assistant_metadata,
                    )
                await self._emit(
                    "message_end",
                    role="assistant",
                    text=output,
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
                return RunResult(output=output, events=list(self._event_log))
            logger.opt(exception=exc).error(
                "Runtime agent turn failed "
                "(session_key={}, route_kind={}, internal_source={})",
                self._state.session_key,
                str(getattr(self, "_last_route_kind", "") or ""),
                internal_source or "user",
            )
            self._state.is_running = False
            self._state.status = "error"
            error = StructuredError(code="legacy_session_error", message=str(exc), recoverable=True)
            self._state.last_error = error
            error_reply = f"运行出错：{error.message}"
            self._state.latest_message = error_reply
            if persist_transcript:
                assistant_metadata = {
                    "source": "runtime_error",
                    "error_code": error.code,
                    "error_message": error.message,
                    "recoverable": error.recoverable,
                }
                if cron_internal:
                    assistant_metadata["cron_job_id"] = str((user_input.metadata or {}).get("cron_job_id") or "").strip()
                await self._persist_turn_transcript(
                    user_input=user_input,
                    user_text=user_text,
                    assistant_text=error_reply,
                    interaction_flow=interaction_flow,
                    internal_source=internal_source,
                    route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                    assistant_metadata=assistant_metadata,
                )
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
            assistant = AssistantMessage(content=output, timestamp=self._now())
            self._state.messages.append(assistant)
            self._state.latest_message = output
            self._state.is_running = False
            self._state.status = "completed"
            self._state.pending_tool_calls.clear()
            user_text = self._history_text(user_input.content)
            interaction_flow = self._interaction_flow_snapshot()
            if getattr(self._loop, "prompt_trace", False):
                logger.info(render_output_trace(output))
            persisted_session = None
            if persist_transcript:
                assistant_metadata = None
                if cron_internal:
                    assistant_metadata = {
                        "source": "cron",
                        "cron_job_id": str((user_input.metadata or {}).get("cron_job_id") or "").strip(),
                    }
                persisted_session = await self._persist_turn_transcript(
                    user_input=user_input,
                    user_text=user_text,
                    assistant_text=output,
                    interaction_flow=interaction_flow,
                    internal_source=internal_source,
                    route_kind=str(getattr(self, "_last_route_kind", "") or ""),
                    assistant_metadata=assistant_metadata,
                )
                if internal_source is None and getattr(self._loop, "memory_manager", None) is not None:
                    batch_messages = [
                        {"role": "user", "content": self._history_text(item.content)}
                        for item in self._current_user_batch_inputs(user_input)
                        if self._history_text(item.content).strip()
                    ]
                    try:
                        await self._loop.memory_manager.ingest_turn(
                            session_key=self._state.session_key,
                            channel=self._memory_channel,
                            chat_id=self._memory_chat_id,
                            messages=[
                                *batch_messages,
                                {"role": "assistant", "content": output},
                            ],
                        )
                    except Exception:
                        await self._emit(
                            "message_delta",
                            channel="analysis",
                            kind="persistence_warning",
                            text="Memory ingest failed; turn history is still available in session transcript.",
                        )
            await self._emit(
                "message_end",
                role="assistant",
                text=output,
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
            return RunResult(output=output, events=list(self._event_log))
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

    async def prompt_batch(
        self,
        messages: list[str | UserInputMessage],
        *,
        persist_transcript: bool = True,
        live_context: dict[str, str] | None = None,
    ) -> RunResult:
        normalized_inputs: list[UserInputMessage] = []
        for raw in list(messages or []):
            item = raw if isinstance(raw, UserInputMessage) else UserInputMessage(content=str(raw))
            if self._internal_prompt_source(item) is not None:
                raise ValueError("prompt_batch_user_messages_only")
            text = self._history_text(item.content)
            if not text.strip() and not item.attachments:
                continue
            normalized_inputs.append(item)
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

    async def continue_(self, *, live_context: dict[str, str] | None = None) -> RunResult:
        return await self.prompt(self._last_prompt, live_context=live_context)

    def steer(self, message: str | UserInputMessage) -> None:
        content = message.content if isinstance(message, UserInputMessage) else str(message)
        self._state.queued_steering_messages.append(UserInputMessage(content=content))

    def follow_up(self, message: str | UserInputMessage) -> None:
        content = message.content if isinstance(message, UserInputMessage) else str(message)
        self._state.queued_follow_up_messages.append(UserInputMessage(content=content))

    async def pause(self, *, manual: bool = False) -> None:
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
        if self._active_cancel_token is not None:
            self._active_cancel_token.cancel(reason="用户已请求暂停，正在安全停止...")
        await self._loop.cancel_session_tasks(self._state.session_key)
        self._state.pending_tool_calls.clear()
        self._pending_tool_call_names.clear()
        self._pending_tool_name_calls.clear()
        self._background_tool_targets.clear()
        self._preserved_inflight_turn = None
        if manual:
            await self._persist_manual_pause_user_messages()
            # Manual pause persists the current prompt's transcript state using the
            # existing turn id so the pending user message can be updated in place.
            # Clear the active turn binding again afterwards so the next real user
            # message starts a fresh transcript turn instead of overwriting the
            # paused request that was just preserved.
            self._active_turn_id = None
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
            self._event_log = []
            self._state.is_running = True
            self._state.paused = False
            self._state.status = "running"
            self._state.latest_message = ""
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
            self._state.latest_message = str(output or "")
            await self._emit(
                "message_end",
                role="assistant",
                text=str(output or ""),
                source="user",
                turn_id=self._current_turn_id(),
            )
            await self._emit_state_snapshot()
            return RunResult(output=str(output or ""), events=list(self._event_log))

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        await self._emit_safe_stop_notice("cancel")
        if self._active_cancel_token is not None:
            self._active_cancel_token.cancel(reason=reason or "用户已请求停止，正在安全停止...")
        await self._loop.cancel_session_tasks(self._state.session_key)
        self._set_manual_pause_waiting_reason(False)
        self._preserved_inflight_turn = None
        self.clear_paused_execution_context()
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
