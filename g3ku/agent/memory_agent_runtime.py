from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, get_type_hints

from loguru import logger
from pydantic import BaseModel, create_model

from g3ku.agent.file_locks import _release_file_lock, _try_acquire_file_lock
from g3ku.agent.markdown_memory import (
    MemoryEntry,
    format_memory_entry,
    note_file_name,
    parse_memory_document,
    validate_memory_document,
)
from g3ku.agent.memory_content_guard import scan_memory_candidate
from g3ku.config.live_runtime import get_runtime_config
from g3ku.providers.base import normalize_usage_payload
from g3ku.providers.chatmodels import build_chat_model
from g3ku.runtime.chunk_compression_plan import resolve_chunk_capacity
from g3ku.runtime.context.summarizer import estimate_tokens
from main.runtime.chat_backend import build_actual_request_diagnostics, resolve_send_model_context_window_info
from main.runtime.send_token_preflight import estimate_runtime_provider_request_preview_tokens
from main.prompts import load_prompt

_NOTE_REF_RE = re.compile(r"(?:\bref:|见noteid:)(?P<ref>[a-z0-9_]+)\b")


def _first_note_ref(text: str) -> str:
    """The note a piece of memory text points at, if any (single-valued by contract)."""
    match = _NOTE_REF_RE.search(str(text or ""))
    return str(match.group("ref") or "").strip() if match else ""

_REVIEW_TOOL_OUTPUT_MAX_CHARS = 400
_REVIEW_TOOL_PREVIEW_MAX_CHARS = 240
_REVIEW_REPORTED_TOOL_ID_CAP = 5000
# 复核窗口入队前的拆片目标：链上最小声明窗口 × 这个比例（不写死绝对 token 数，
# 换模型或改窗口声明都自动跟着走）。
_MEMORY_WINDOW_SPLIT_RATIO = 0.5
_STAGED_DOCUMENT_UNSET = object()
# 记忆三类读面（队列 / 已处理批次 / 失败停车区）共用的载荷窗口，字符寻址，与
# g3ku/content/navigation 的 char 模式同词汇。存储不动：三份文件里仍是完整载荷，
# 回队、重建快照、审计都读原文，投影只发生在返回浏览器的那一份拷贝上。
# 首屏 4000 字符：抽屉正文的成本是排版，实盘一条 1.28MB 载荷让"点开一张卡片"占住主线程 2.9 秒。
# 一屏 20000 字符而不是导航车道的 128000：那一档是给模型读文件的，操作者面每屏只要排版得起。
_MEMORY_PAYLOAD_PREVIEW_CHARS = 4_000
_MEMORY_PAYLOAD_WINDOW_CHARS = 20_000
_DELETE_NOOP_RULE = (
    "delete batch may not use noop_reason; submit the resolved ids in deletes, "
    "or declare already_satisfied when the delete target is no longer in the snapshot"
)
_LEGACY_CLEANUP_RELATIVE_PATHS: tuple[str, ...] = (
    "memory/HISTORY.md",
    "memory/structured_current.jsonl",
    "memory/structured_history.jsonl",
    "memory/structured_state.json",
    "memory/sync_journal.jsonl",
    "memory/sync_state.json",
    "memory/audit.jsonl",
    "memory/pending_facts.jsonl",
    "memory/context_store",
)


@dataclass(slots=True)
class MemoryQueueRequest:
    op: str
    decision_source: str
    payload_text: str
    created_at: str
    request_id: str = ""
    trigger_source: str = ""
    session_key: str = ""
    status: str = "pending"
    processing_started_at: str = ""
    last_error_text: str = ""
    last_error_at: str = ""
    retry_after: str = ""


@dataclass(slots=True)
class MemoryBatch:
    op: str
    items: list[MemoryQueueRequest] = field(default_factory=list)


@dataclass(slots=True)
class MemoryPayloadClassification:
    decision_source: str
    memory_kind: str
    enqueue: bool
    replace_mode: str
    reason: str
    normalized_payload_text: str


@dataclass(slots=True)
class AutonomousReviewDecision:
    enqueue: bool
    reason: str
    decision_source: str
    memory_kind: str
    payload_text: str
    sampled: bool = False


class MemoryStrategyV2:
    _IGNORE_TRIGGER_SOURCES = {
        "pre_compression_flush",
        "session_boundary_flush",
    }
    _USER_IDENTITY_PATTERNS = (
        re.compile(r"(?:我叫|我的名字是|我来自|我住在|我的身份是)"),
        re.compile(
            r"\b(?:my name is|i live in|i'm based in|my role is|my identity is|i work on project|i work on the project)\b",
            re.IGNORECASE,
        ),
    )
    _USER_PREFERENCE_PATTERNS = (
        re.compile(r"(?:记住|请记住).*(?:以后|默认|优先|偏好|请用|不要用|总是|一律)"),
        re.compile(r"(?:以后|今后|默认|优先|偏好|请用中文|用中文回复|别用英文)"),
        re.compile(r"\b(?:prefer|default to|by default|from now on|always use|remember that)\b", re.IGNORECASE),
    )
    _USER_CONSTRAINT_PATTERNS = (
        re.compile(r"(?:记住|请记住).*(?:不要|别|必须|先|禁止)"),
        re.compile(r"(?:以后|今后|默认|始终|一律).*(?:不要|别|必须|先|禁止)"),
        re.compile(r"\b(?:from now on|going forward|always|never|by default|remember that)\b.*\b(?:must|do not|don't|never)\b", re.IGNORECASE),
    )
    _SELF_RULE_PATTERNS = (
        re.compile(r"\b(?:report|summarize|include|mention|record)\b", re.IGNORECASE),
        re.compile(r"(?:说明|汇总|报告|写明|包含|记录)"),
    )
    _SELF_TRANSIENT_PATTERNS = (
        re.compile(r"session boundary flush", re.IGNORECASE),
        re.compile(r"pre[-_ ]compression", re.IGNORECASE),
        re.compile(r"\b(?:runtime state|pause state|currently processing|still processing|in progress)\b", re.IGNORECASE),
        re.compile(r"\b(?:processing_started_at|retry_after|last_error_text|request_id|status=pending|status=processing)\b", re.IGNORECASE),
        re.compile(r"(?:会话边界|压缩前刷新|暂停态|正在处理中|仍在处理中|请求编号|重试时间)"),
    )
    _TASK_LOCAL_PATTERNS = (
        re.compile(r"\b(?:attached diff|this change|for this change|this task|this turn|for now|later|yet)\b", re.IGNORECASE),
        re.compile(r"\b(?:i'll do that later|i will do that later|don't run tests yet|do not run tests yet)\b", re.IGNORECASE),
        re.compile(r"\b(?:use the attached|wait for me to|i'll handle|i will handle)\b", re.IGNORECASE),
        re.compile(r"\b(?:right now|today|this command|this one|working on|debugging|currently debugging|currently working on)\b", re.IGNORECASE),
        re.compile(r"\b(?:repo root|bash for this one|for this command)\b", re.IGNORECASE),
        re.compile(r"(?:这个改动|这次改动|这个任务|这一轮|稍后|待会|先不要|附件 diff|附带 diff)"),
    )

    def __init__(self, config: Any):
        self.config = config
        self._review_interval_turns = max(int(getattr(config.queue, "review_interval_turns", 10) or 10), 1)
        self._document_max_chars = max(int(getattr(config.document, "document_max_chars", 20000) or 20000), 1)

    def classify_memory_payload(
        self,
        *,
        decision_source: str,
        payload_text: str,
        trigger_source: str = "",
        op: str = "write",
    ) -> MemoryPayloadClassification:
        normalized_source = "user" if str(decision_source or "").strip().lower() == "user" else "self"
        normalized_payload = str(payload_text or "").strip()
        normalized_trigger = str(trigger_source or "").strip().lower()
        if not normalized_payload:
            return MemoryPayloadClassification(
                decision_source=normalized_source,
                memory_kind="empty",
                enqueue=False,
                replace_mode="ignore",
                reason="empty_payload",
                normalized_payload_text="",
            )
        if normalized_source == "self" and self._should_ignore_self_memory(
            payload_text=normalized_payload,
            trigger_source=normalized_trigger,
        ):
            return MemoryPayloadClassification(
                decision_source="self",
                memory_kind="self_ignored",
                enqueue=False,
                replace_mode="ignore",
                reason="self_memory_ignored",
                normalized_payload_text=normalized_payload,
            )

        memory_kind = self._classify_kind(normalized_payload)
        if normalized_source == "self" and self._is_task_local_instruction(normalized_payload):
            memory_kind = "self_general"
        promoted_source = normalized_source
        if normalized_source == "self" and memory_kind.startswith("user_"):
            promoted_source = "user"
        elif normalized_source == "user" and memory_kind.startswith("self_"):
            memory_kind = "user_general"

        return MemoryPayloadClassification(
            decision_source=promoted_source,
            memory_kind=memory_kind,
            enqueue=True,
            replace_mode=self._replace_mode(memory_kind=memory_kind, op=op),
            reason="classified",
            normalized_payload_text=normalized_payload,
        )

    def should_enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
    ) -> AutonomousReviewDecision:
        normalized_user_messages = [str(item or "").strip() for item in list(user_messages or []) if str(item or "").strip()]
        normalized_assistant = str(assistant_text or "").strip()
        parts = list(normalized_user_messages)
        if normalized_assistant:
            parts.append(normalized_assistant)
        payload_text = "\n".join(parts).strip()
        if not payload_text:
            return AutonomousReviewDecision(
                enqueue=False,
                reason="empty_turn",
                decision_source="self",
                memory_kind="empty",
                payload_text="",
                sampled=False,
            )

        direct_user = self.classify_memory_payload(
            decision_source="self",
            payload_text="\n".join(normalized_user_messages).strip(),
            trigger_source=f"autonomous_review:{str(turn_id or '').strip()}",
        )
        if direct_user.enqueue and direct_user.decision_source == "user":
            return AutonomousReviewDecision(
                enqueue=True,
                reason="direct_user_memory",
                decision_source=direct_user.decision_source,
                memory_kind=direct_user.memory_kind,
                payload_text=payload_text,
                sampled=False,
            )

        if len(payload_text) < 24:
            return AutonomousReviewDecision(
                enqueue=False,
                reason="turn_too_small",
                decision_source="self",
                memory_kind="self_general",
                payload_text=payload_text,
                sampled=False,
            )

        sampled = self._sample_review_window(session_key=session_key, turn_id=turn_id)
        if not sampled:
            return AutonomousReviewDecision(
                enqueue=False,
                reason="outside_review_window",
                decision_source="self",
                memory_kind="self_general",
                payload_text=payload_text,
                sampled=False,
            )

        classification = self.classify_memory_payload(
            decision_source="self",
            payload_text=payload_text,
            trigger_source=f"autonomous_review:{str(turn_id or '').strip()}",
        )
        return AutonomousReviewDecision(
            enqueue=classification.enqueue,
            reason="sampled_review_window",
            decision_source=classification.decision_source,
            memory_kind=classification.memory_kind,
            payload_text=classification.normalized_payload_text,
            sampled=True,
        )

    def prioritize_existing_entries(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        ranked: list[tuple[int, int, MemoryEntry]] = []
        for index, entry in enumerate(list(entries or [])):
            classification = self.classify_memory_payload(
                decision_source=str(entry.source or "").strip(),
                payload_text=str(entry.summary or "").strip(),
                trigger_source="memory_snapshot",
            )
            score = self._priority_score(classification.memory_kind)
            if not classification.enqueue:
                score = -100
            ranked.append((score, index, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [entry for _score, _index, entry in ranked]

    def prompt_strategy_snapshot(self, *, batch: MemoryBatch, snapshot_text: str) -> dict[str, Any]:
        existing_entries = parse_memory_document(snapshot_text)
        prioritized_existing = self.prioritize_existing_entries(existing_entries)
        incoming = [
            asdict(
                self.classify_memory_payload(
                    decision_source=item.decision_source,
                    payload_text=item.payload_text,
                    trigger_source=item.trigger_source,
                    op=item.op,
                )
            )
            for item in batch.items
        ]
        return {
            "review_sampling_window": f"1/{self._review_interval_turns} hashed turns unless direct user memory is detected",
            "direct_user_upgrade": [
                "stable identity",
                "stable preference/default",
                "durable workflow constraint",
            ],
            "ignored_self_memory": [
                "session boundary flush",
                "pre-compression payloads",
                "transient runtime or pause state",
            ],
            "near_limit_retention_order": [
                "user identity and hard constraints",
                "user preferences/defaults and long-lived project facts",
                "self workflow rules that still generalize",
                "self transient notes are pruned first",
            ],
            "replace_vs_delete_boundary": {
                "replace_existing": "same user slot or durable rule changed in place",
                "delete_existing": "entry is obsolete without a replacement value",
            },
            "document_max_chars": self._document_max_chars,
            "incoming_classifications": incoming,
            "prioritized_existing_summaries": [str(entry.summary or "") for entry in prioritized_existing[:8]],
        }

    def _classify_kind(self, payload_text: str) -> str:
        if self._is_task_local_instruction(payload_text):
            return "self_general"
        if self._matches_any(self._USER_IDENTITY_PATTERNS, payload_text):
            return "user_identity"
        if self._matches_any(self._USER_PREFERENCE_PATTERNS, payload_text):
            return "user_preference"
        if self._matches_any(self._USER_CONSTRAINT_PATTERNS, payload_text):
            return "user_constraint"
        if self._matches_any(self._SELF_TRANSIENT_PATTERNS, payload_text):
            return "self_transient"
        if self._matches_any(self._SELF_RULE_PATTERNS, payload_text):
            return "self_rule"
        return "self_general"

    def _should_ignore_self_memory(self, *, payload_text: str, trigger_source: str) -> bool:
        if str(trigger_source or "").strip().lower() in self._IGNORE_TRIGGER_SOURCES:
            return True
        stripped = str(payload_text or "").strip()
        if not stripped:
            return True
        if stripped.startswith("[") and '"role"' in stripped and '"content"' in stripped:
            return True
        return self._matches_any(self._SELF_TRANSIENT_PATTERNS, stripped)

    def _is_task_local_instruction(self, payload_text: str) -> bool:
        stripped = str(payload_text or "").strip()
        if not stripped:
            return False
        if self._matches_any(self._TASK_LOCAL_PATTERNS, stripped):
            return True
        # Temporary test-deferral or patch-routing instructions are task-local even if they
        # contain "don't" or "please use", so they must not become durable user memory.
        if re.search(r"\b(?:run tests?|use)\b", stripped, re.IGNORECASE) and re.search(
            r"\b(?:later|yet|attached diff|this change|this command|this one)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        if re.search(r"\b(?:debugging|working on)\b", stripped, re.IGNORECASE) and re.search(
            r"\b(?:right now|today|currently|this)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        return False

    def _replace_mode(self, *, memory_kind: str, op: str) -> str:
        if str(op or "").strip().lower() == "delete":
            return "delete_existing"
        if memory_kind in {"user_identity", "user_preference", "user_constraint", "self_rule"}:
            return "replace_existing"
        if memory_kind == "self_ignored":
            return "ignore"
        return "append_or_merge"

    @staticmethod
    def _priority_score(memory_kind: str) -> int:
        score_map = {
            "user_identity": 500,
            "user_constraint": 450,
            "user_preference": 400,
            "user_general": 320,
            "self_rule": 220,
            "self_general": 120,
            "self_transient": -20,
            "self_ignored": -100,
            "empty": -200,
        }
        return int(score_map.get(memory_kind, 100))

    def _sample_review_window(self, *, session_key: str, turn_id: str) -> bool:
        sample_key = f"{str(session_key or '').strip()}:{str(turn_id or '').strip()}".encode("utf-8")
        digest = hashlib.sha1(sample_key).hexdigest()
        return int(digest[:8], 16) % self._review_interval_turns == 0

    @staticmethod
    def _matches_any(patterns: tuple[Any, ...], payload_text: str) -> bool:
        return any(pattern.search(str(payload_text or "")) for pattern in patterns)
def _strip_schema_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _strip_schema_titles(value) for key, value in node.items() if key != "title"}
    if isinstance(node, list):
        return [_strip_schema_titles(item) for item in node]
    return node


def _memory_tool_validator(fn: Callable[..., Any], model_name: str) -> type[BaseModel]:
    """Build the argument validator straight from the handler signature.

    The field defaults come from the signature, so an omitted optional stays optional
    and a bare parameter stays required; nothing has to be kept in sync by hand.
    """
    hints = get_type_hints(fn)
    fields: dict[str, tuple[Any, Any]] = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        fields[name] = (
            hints.get(name, Any),
            ... if parameter.default is inspect.Parameter.empty else parameter.default,
        )
    return create_model(model_name, **fields)


@dataclass(frozen=True, slots=True)
class _MemoryTool:
    name: str
    description: str
    parameters: dict[str, Any]
    validator: type[BaseModel]
    handler: Callable[..., Any]

    @property
    def args(self) -> dict[str, Any]:
        return dict(self.parameters.get("properties") or {})

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def invoke(self, args: dict[str, Any]) -> Any:
        validated = self.validator.model_validate(dict(args or {}))
        return self.handler(**validated.model_dump())


def _build_memory_tool(*, name: str, fn: Callable[..., Any]) -> _MemoryTool:
    validator = _memory_tool_validator(fn, name)
    parameters = _strip_schema_titles(validator.model_json_schema())
    parameters.pop("description", None)
    return _MemoryTool(
        name=name,
        description=inspect.cleandoc(str(fn.__doc__ or "")).strip(),
        parameters=parameters,
        validator=validator,
        handler=fn,
    )




@dataclass(slots=True)
class _MemoryToolSession:
    snapshot_text: str
    notes_dir: Path
    batch_op: str = ""
    staged_document: object = _STAGED_DOCUMENT_UNSET
    staged_notes: dict[str, str] = field(default_factory=dict)
    applied_batch: dict[str, Any] | None = None
    apply_batch_count: int = 0

    def read_document(self) -> str:
        if self.staged_document is _STAGED_DOCUMENT_UNSET:
            return str(self.snapshot_text or "")
        return str(self.staged_document or "")

    def read_note(self, ref: str) -> str:
        normalized = str(ref or "").strip()
        if not normalized:
            raise ValueError("note ref is required")
        if normalized in self.staged_notes:
            return self.staged_notes[normalized]
        path = self.notes_dir / note_file_name(normalized)
        if not path.exists():
            raise FileNotFoundError(f"memory note not found: {normalized}")
        return path.read_text(encoding="utf-8")

    def write_document(self, content: str) -> str:
        self.staged_document = str(content or "")
        return "document staged"

    def write_note(self, ref: str, content: str) -> str:
        normalized = str(ref or "").strip()
        note_file_name(normalized)
        self.staged_notes[normalized] = str(content or "")
        return f"note staged: {normalized}"

    def apply_batch(
        self,
        *,
        adds: list[dict[str, Any]] | None = None,
        rewrites: list[dict[str, Any]] | None = None,
        deletes: list[str] | None = None,
        note_upserts: dict[str, str] | None = None,
        inspired_memory_ids: list[str] | None = None,
        noop_reason: str | None = None,
        already_satisfied: str | None = None,
    ) -> dict[str, Any]:
        errors: dict[str, str] = {}
        normalized_adds: list[dict[str, str]] = []
        normalized_rewrites: list[dict[str, str]] = []
        normalized_deletes: list[str] = []
        normalized_note_upserts: dict[str, str] = {}
        normalized_inspired_memory_ids: list[str] = []
        normalized_noop_reason = str(noop_reason or "").strip()
        normalized_already_satisfied = str(already_satisfied or "").strip()

        raw_adds = list(adds or [])
        if not isinstance(raw_adds, list):
            errors["adds"] = "adds must be a list"
        else:
            for index, item in enumerate(raw_adds):
                if not isinstance(item, dict):
                    errors[f"adds[{index}]"] = "add item must be an object"
                    continue
                content = str(item.get("content") or "").strip()
                minimal_memory = str(item.get("minimal_memory") or "").strip()
                decision_source = str(item.get("decision_source") or "").strip().lower()
                if not content:
                    errors[f"adds[{index}].content"] = "content must not be empty"
                if content and not minimal_memory:
                    errors[f"adds[{index}].minimal_memory"] = "minimal_memory must not be empty"
                if decision_source not in {"user", "self"}:
                    errors[f"adds[{index}].decision_source"] = "decision_source must be user or self"
                if content and minimal_memory and decision_source in {"user", "self"}:
                    normalized_adds.append(
                        {
                            "content": content,
                            "minimal_memory": minimal_memory,
                            "decision_source": decision_source,
                        }
                    )

        raw_rewrites = list(rewrites or [])
        if not isinstance(raw_rewrites, list):
            errors["rewrites"] = "rewrites must be a list"
        else:
            for index, item in enumerate(raw_rewrites):
                if not isinstance(item, dict):
                    errors[f"rewrites[{index}]"] = "rewrite item must be an object"
                    continue
                memory_id = str(item.get("id") or "").strip()
                content = str(item.get("content") or "").strip()
                minimal_memory = str(item.get("minimal_memory") or "").strip()
                if not memory_id:
                    errors[f"rewrites[{index}].id"] = "id must not be empty"
                if not content:
                    errors[f"rewrites[{index}].content"] = "content must not be empty"
                if content and not minimal_memory:
                    errors[f"rewrites[{index}].minimal_memory"] = "minimal_memory must not be empty"
                if memory_id and content and minimal_memory:
                    normalized_rewrites.append(
                        {
                            "id": memory_id,
                            "content": content,
                            "minimal_memory": minimal_memory,
                        }
                    )

        raw_deletes = list(deletes or [])
        if not isinstance(raw_deletes, list):
            errors["deletes"] = "deletes must be a list"
        else:
            for index, item in enumerate(raw_deletes):
                memory_id = str(item or "").strip()
                if not memory_id:
                    errors[f"deletes[{index}]"] = "delete id must not be empty"
                    continue
                normalized_deletes.append(memory_id)

        raw_note_upserts = dict(note_upserts or {})
        if not isinstance(raw_note_upserts, dict):
            errors["note_upserts"] = "note_upserts must be an object"
        else:
            for raw_ref, raw_body in raw_note_upserts.items():
                ref = str(raw_ref or "").strip()
                body = str(raw_body or "")
                try:
                    note_file_name(ref)
                except Exception as exc:
                    errors[f"note_upserts.{ref or '<empty>'}"] = str(exc or "invalid note ref").strip()
                    continue
                normalized_note_upserts[ref] = body

        raw_inspired_memory_ids = inspired_memory_ids
        if raw_inspired_memory_ids is None:
            raw_inspired_memory_ids = []
        elif not isinstance(raw_inspired_memory_ids, list):
            errors["inspired_memory_ids"] = "inspired_memory_ids must be a list"
        else:
            seen_inspired_ids: set[str] = set()
            for index, item in enumerate(raw_inspired_memory_ids):
                memory_id = str(item or "").strip()
                if not memory_id:
                    errors[f"inspired_memory_ids[{index}]"] = "memory id must not be empty"
                    continue
                if memory_id in seen_inspired_ids:
                    continue
                seen_inspired_ids.add(memory_id)
                normalized_inspired_memory_ids.append(memory_id)

        has_mutation = bool(normalized_adds or normalized_rewrites or normalized_deletes or normalized_note_upserts)
        if normalized_noop_reason and has_mutation:
            errors["noop_reason"] = "noop_reason may not be combined with add, rewrite, delete, or note_upsert"
        if normalized_already_satisfied and (has_mutation or normalized_noop_reason):
            errors["already_satisfied"] = (
                "already_satisfied may not be combined with add, rewrite, delete, note_upsert, or noop_reason"
            )
        if self.batch_op == "delete" and normalized_noop_reason:
            errors["noop_reason"] = _DELETE_NOOP_RULE
        if self.batch_op != "delete" and normalized_already_satisfied:
            errors["already_satisfied"] = "already_satisfied is only valid for delete batches"
        if not normalized_noop_reason and not normalized_already_satisfied and not has_mutation:
            errors["batch"] = (
                "at least one add, rewrite, delete, note_upsert, noop_reason, or already_satisfied is required"
            )

        if errors:
            return {"ok": False, "errors": errors}

        self.apply_batch_count += 1
        self.applied_batch = {
            "adds": normalized_adds,
            "rewrites": normalized_rewrites,
            "deletes": normalized_deletes,
            "note_upserts": normalized_note_upserts,
            "inspired_memory_ids": normalized_inspired_memory_ids,
            "noop_reason": normalized_noop_reason,
            "already_satisfied": normalized_already_satisfied,
        }
        return {"ok": True, "status": "batch_staged"}


@dataclass(slots=True)
class _MemoryAttemptResult:
    session: _MemoryToolSession
    usage: dict[str, int]
    final_text: str
    request_artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _MemoryValidatedWrite:
    document_text: str
    note_writes: dict[str, str]
    note_refs_written: list[str]
    memory_chars_after: int
    document_preview: str
    adds: list[dict[str, str]] = field(default_factory=list)
    rewrites: list[dict[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    note_upserts: dict[str, str] = field(default_factory=dict)
    inspired_memory_ids: list[str] = field(default_factory=list)
    write_mode: str = ""
    # Note files this batch deleted because the store stopped owning them, each with
    # the reason the ownership went away.
    reaped_notes: list[dict[str, Any]] = field(default_factory=list)
    # Only an explicit operator legacy import may load sqlite from a document.
    adopt_document: bool = False
    noop_reason: str = ""
    already_satisfied: str = ""
    compression_triggered: bool = False
    compressed_memory_ids: list[str] = field(default_factory=list)
    deleted_by_compression_ids: list[str] = field(default_factory=list)
    # Original memory bodies (memory_id -> body) captured before a rewrite/delete
    # is applied, so the processed record can show an original vs modified comparison.
    original_bodies: dict[str, str] = field(default_factory=dict)


class _MemoryAgentValidationError(ValueError):
    pass


class _MemoryAgentRuntimeError(RuntimeError):
    pass


class _MemoryProviderError(RuntimeError):
    """发送/调用层失败，与"模型没按协议调工具"区分开。

    携带失败前已累计的 usage 与 request artifacts，供停车记录保留真实成本与线索。
    `discard_reason` 决定停车类别：provider 回包错误按 `provider_error`（瞬时类，
    参与成功信号自动重排），本地发送前预检拒发按 `window_refusal`（确定性失败，
    重试只会重演，等载荷或模型窗口配置变化后人工重试）。
    """

    def __init__(
        self,
        error_text: str,
        *,
        usage: dict[str, int] | None = None,
        request_artifacts: list[dict[str, Any]] | None = None,
        discard_reason: str = "provider_error",
    ):
        super().__init__(error_text)
        self.error_text = str(error_text or "").strip()
        self.usage = dict(usage or {})
        self.request_artifacts = list(request_artifacts or [])
        self.discard_reason = str(discard_reason or "provider_error").strip()


class _MemorySqliteRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    memory_body TEXT NOT NULL,
                    minimal_memory TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('user', 'self')),
                    from_user INTEGER NOT NULL DEFAULT 0,
                    refresh_count INTEGER NOT NULL DEFAULT 0,
                    passed_count INTEGER NOT NULL DEFAULT 0,
                    is_compressed INTEGER NOT NULL DEFAULT 0,
                    note_ref TEXT NOT NULL DEFAULT '',
                    consolidated_into TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_compression
                ON memories(passed_count DESC, refresh_count ASC)
                """
            )
            # Stores created before these columns existed get them in place; SQLite has
            # no IF NOT EXISTS for ADD COLUMN, so probe first and keep it re-runnable.
            present = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)")}
            for column in ("note_ref", "consolidated_into"):
                if column not in present:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            conn.commit()
        finally:
            conn.close()

    def backfill_note_refs(self) -> int:
        """Move note ownership out of the free text and into the column, once.

        Ownership of a note file must not depend on whether the row happens to render a
        marker: the projection is lossy for compressed rows (they show the minimal
        string), and a rewrite that paraphrases a rule can drop the marker on the way.
        Idempotent - it only fills rows whose column is still empty.
        """
        conn = self._connect()
        filled = 0
        try:
            with conn:
                for row in conn.execute(
                    "SELECT memory_id, memory_body, minimal_memory FROM memories WHERE note_ref = ''"
                ).fetchall():
                    ref = _first_note_ref(str(row["minimal_memory"] or "")) or _first_note_ref(str(row["memory_body"] or ""))
                    if not ref:
                        continue
                    conn.execute("UPDATE memories SET note_ref = ? WHERE memory_id = ?", (ref, str(row["memory_id"])))
                    filled += 1
        finally:
            conn.close()
        return filled

    def referenced_note_refs(self) -> list[str]:
        """Every note name the store says is in use, including retired rows.

        Retired (`consolidated_into`) rows keep their `note_ref` on purpose: a note the
        consolidation lane merely points at is still owned, and reading ownership from
        the rendered document would sweep it the moment the row stops rendering.
        """
        conn = self._connect()
        try:
            refs: set[str] = set()
            for row in conn.execute("SELECT note_ref, consolidated_into FROM memories").fetchall():
                for value in (str(row["note_ref"] or "").strip(), str(row["consolidated_into"] or "").strip()):
                    if value:
                        refs.add(value)
            return sorted(refs)
        finally:
            conn.close()

    def _generate_id(self, conn: sqlite3.Connection) -> str:
        alphabet = string.ascii_letters + string.digits
        for _ in range(2048):
            candidate = "".join(secrets.choice(alphabet) for _ in range(6))
            exists = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id = ?",
                (candidate,),
            ).fetchone()
            if exists is None:
                return candidate
        raise RuntimeError("unable to allocate unique memory id")

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["from_user"] = bool(payload.get("from_user"))
        payload["is_compressed"] = bool(payload.get("is_compressed"))
        return payload

    @staticmethod
    def _resolve_note_ref(item: dict[str, Any]) -> str:
        """Explicit `note_ref` wins; otherwise derive it from the text being written.

        Deriving is a migration convenience, not the authority: once stored, the column
        is what protects the note file, because a later rewrite may drop the marker.
        """
        explicit = str(item.get("note_ref") or "").strip()
        if explicit:
            return explicit
        content = str(item.get("content") or "")
        minimal = str(item.get("minimal_memory") or "")
        return _first_note_ref(minimal) or _first_note_ref(content)

    @staticmethod
    def _normalize_source_and_user_flag(source: str, from_user: bool) -> tuple[str, bool]:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in {"user", "self"}:
            raise ValueError("source must be user or self")
        normalized_from_user = normalized_source == "user"
        if bool(from_user) != normalized_from_user:
            raise ValueError("from_user must match source")
        return normalized_source, normalized_from_user

    def create_memory(
        self,
        *,
        memory_body: str,
        minimal_memory: str,
        source: str,
        from_user: bool,
        now_iso: str,
    ) -> dict[str, Any]:
        normalized_source, normalized_from_user = self._normalize_source_and_user_flag(source, from_user)
        conn = self._connect()
        try:
            memory_id = self._generate_id(conn)
            conn.execute(
                """
                INSERT INTO memories(
                    memory_id,
                    memory_body,
                    minimal_memory,
                    source,
                    from_user,
                    refresh_count,
                    passed_count,
                    is_compressed,
                    note_ref,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                """,
                (
                    memory_id,
                    memory_body,
                    minimal_memory,
                    normalized_source,
                    1 if normalized_from_user else 0,
                    _first_note_ref(minimal_memory) or _first_note_ref(memory_body),
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"failed to read back memory row: {memory_id}")
            return self._row_dict(row)
        finally:
            conn.close()

    @staticmethod
    def _mutation_timestamp(now_iso: str, offset: int = 0) -> str:
        base = datetime.fromisoformat(str(now_iso).strip())
        if int(offset or 0) <= 0:
            return base.isoformat()
        return (base + timedelta(microseconds=int(offset))).isoformat()

    @classmethod
    def _snapshot_timestamp(cls, date_text: str, fallback_iso: str, offset: int = 0) -> str:
        stripped = str(date_text or "").strip()
        try:
            year_text, month_text, day_text = stripped.split("/", 2)
            base = datetime(int(year_text), int(month_text), int(day_text))
        except Exception:
            return cls._mutation_timestamp(fallback_iso, offset)
        if int(offset or 0) <= 0:
            return base.isoformat()
        return (base + timedelta(microseconds=int(offset))).isoformat()

    def replace_all_from_document(self, *, document_text: str, now_iso: str) -> None:
        entries = parse_memory_document(str(document_text or ""))
        conn = self._connect()
        try:
            existing_rows = {
                str(row["memory_id"]): self._row_dict(row)
                for row in conn.execute("SELECT * FROM memories").fetchall()
            }
            with conn:
                conn.execute("DELETE FROM memories")
                for index, entry in enumerate(entries):
                    normalized_source, normalized_from_user = self._normalize_source_and_user_flag(
                        str(entry.source or ""),
                        str(entry.source or "").strip().lower() == "user",
                    )
                    existing_row = dict(existing_rows.get(str(entry.memory_id or "").strip()) or {})
                    summary = str(entry.summary or "").strip()
                    timestamp = self._snapshot_timestamp(str(entry.date_text or ""), now_iso, index)
                    conn.execute(
                        """
                        INSERT INTO memories(
                            memory_id,
                            memory_body,
                            minimal_memory,
                            source,
                            from_user,
                            refresh_count,
                            passed_count,
                            is_compressed,
                            note_ref,
                            consolidated_into,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(entry.memory_id or "").strip(),
                            str(existing_row.get("memory_body") or "").strip() or summary,
                            summary,
                            normalized_source,
                            1 if normalized_from_user else 0,
                            int(existing_row.get("refresh_count", 0) or 0),
                            int(existing_row.get("passed_count", 0) or 0),
                            1 if bool(existing_row.get("is_compressed")) else 0,
                            str(existing_row.get("note_ref") or "").strip() or str(entry.note_ref or "").strip(),
                            str(existing_row.get("consolidated_into") or "").strip(),
                            str(existing_row.get("created_at") or timestamp),
                            timestamp,
                        ),
                    )
        finally:
            conn.close()

    def restore_rows(self, *, rows_before: list[dict[str, Any]]) -> None:
        """Roll the store back to a previously read row set, column for column.

        Used when the mirror rejects a batch after sqlite already took it: the applied
        mutation has to be undone exactly, not re-derived from a rendered document
        (rendering is lossy - a compressed row renders as its minimal string).
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM memories")
                for row in list(rows_before or []):
                    conn.execute(
                        """
                        INSERT INTO memories(
                            memory_id,
                            memory_body,
                            minimal_memory,
                            source,
                            from_user,
                            refresh_count,
                            passed_count,
                            is_compressed,
                            note_ref,
                            consolidated_into,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("memory_id") or ""),
                            str(row.get("memory_body") or ""),
                            str(row.get("minimal_memory") or ""),
                            str(row.get("source") or ""),
                            1 if bool(row.get("from_user")) else 0,
                            int(row.get("refresh_count", 0) or 0),
                            int(row.get("passed_count", 0) or 0),
                            1 if bool(row.get("is_compressed")) else 0,
                            str(row.get("note_ref") or ""),
                            str(row.get("consolidated_into") or ""),
                            str(row.get("created_at") or ""),
                            str(row.get("updated_at") or ""),
                        ),
                    )
        finally:
            conn.close()

    def apply_mutation(
        self,
        *,
        adds: list[dict[str, Any]] | None,
        rewrites: list[dict[str, Any]] | None,
        deletes: list[str] | None,
        inspired_memory_ids: list[str] | None,
        now_iso: str,
    ) -> None:
        normalized_adds = list(adds or [])
        normalized_rewrites = list(rewrites or [])
        delete_ids = {str(item or "").strip() for item in list(deletes or []) if str(item or "").strip()}
        rewrite_ids = {str(item.get("id") or "").strip() for item in normalized_rewrites if str(item.get("id") or "").strip()}
        refreshed_ids = {
            str(item or "").strip()
            for item in list(inspired_memory_ids or [])
            if str(item or "").strip()
        } | rewrite_ids
        conn = self._connect()
        try:
            with conn:
                added_ids: set[str] = set()
                for index, item in enumerate(normalized_adds):
                    decision_source = str(item.get("decision_source") or "").strip().lower()
                    normalized_source, normalized_from_user = self._normalize_source_and_user_flag(
                        decision_source,
                        decision_source == "user",
                    )
                    memory_id = str(item.get("id") or item.get("memory_id") or "").strip() or self._generate_id(conn)
                    timestamp = self._mutation_timestamp(now_iso, index)
                    add_note_ref = self._resolve_note_ref(item)
                    conn.execute(
                        """
                        INSERT INTO memories(
                            memory_id,
                            memory_body,
                            minimal_memory,
                            source,
                            from_user,
                            refresh_count,
                            passed_count,
                            is_compressed,
                            note_ref,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                        """,
                        (
                            memory_id,
                            str(item.get("content") or ""),
                            str(item.get("minimal_memory") or ""),
                            normalized_source,
                            1 if normalized_from_user else 0,
                            add_note_ref,
                            timestamp,
                            timestamp,
                        ),
                    )
                    added_ids.add(memory_id)

                for item in normalized_rewrites:
                    memory_id = str(item.get("id") or "").strip()
                    if not memory_id:
                        continue
                    # A rewrite that paraphrases the rule without repeating the marker
                    # keeps the note it already owned: ownership lives in the column,
                    # not in whether this particular wording carried the marker.
                    rewrite_note_ref = self._resolve_note_ref(item)
                    conn.execute(
                        """
                        UPDATE memories
                        SET
                            memory_body = ?,
                            minimal_memory = ?,
                            note_ref = CASE WHEN ? != '' THEN ? ELSE note_ref END,
                            refresh_count = refresh_count + 1,
                            passed_count = 0,
                            is_compressed = 0,
                            updated_at = ?
                        WHERE memory_id = ?
                        """,
                        (
                            str(item.get("content") or ""),
                            str(item.get("minimal_memory") or ""),
                            rewrite_note_ref,
                            rewrite_note_ref,
                            now_iso,
                            memory_id,
                        ),
                    )

                for memory_id in delete_ids:
                    conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))

                refresh_only_ids = sorted(refreshed_ids - rewrite_ids - delete_ids)
                if refresh_only_ids:
                    placeholders = ", ".join("?" for _ in refresh_only_ids)
                    conn.execute(
                        f"""
                        UPDATE memories
                        SET
                            refresh_count = refresh_count + 1,
                            passed_count = 0
                        WHERE memory_id IN ({placeholders})
                        """,
                        tuple(refresh_only_ids),
                    )

                passed_exclusions = sorted(refreshed_ids | delete_ids | added_ids)
                if passed_exclusions:
                    placeholders = ", ".join("?" for _ in passed_exclusions)
                    conn.execute(
                        f"UPDATE memories SET passed_count = passed_count + 1 WHERE memory_id NOT IN ({placeholders})",
                        tuple(passed_exclusions),
                    )
                else:
                    conn.execute("UPDATE memories SET passed_count = passed_count + 1")
        finally:
            conn.close()

    def list_memories(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM memories ORDER BY created_at, memory_id").fetchall()
            return [self._row_dict(row) for row in rows]
        finally:
            conn.close()

    def fetch_bodies(self, memory_ids: list[str]) -> dict[str, str]:
        """Return {memory_id: memory_body} for the given ids (missing ids omitted)."""
        normalized_ids = sorted({str(item or "").strip() for item in list(memory_ids or []) if str(item or "").strip()})
        if not normalized_ids:
            return {}
        conn = self._connect()
        try:
            placeholders = ", ".join("?" for _ in normalized_ids)
            rows = conn.execute(
                f"SELECT memory_id, memory_body FROM memories WHERE memory_id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            return {str(row["memory_id"]): str(row["memory_body"] or "") for row in rows}
        finally:
            conn.close()

    def compression_candidates(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM memories
                ORDER BY passed_count DESC, refresh_count ASC, created_at ASC, memory_id ASC
                """
            ).fetchall()
            return [self._row_dict(row) for row in rows]
        finally:
            conn.close()

    def mark_compressed(self, memory_id: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE memories SET is_compressed = 1 WHERE memory_id = ? AND is_compressed = 0",
                (str(memory_id or "").strip(),),
            )
            conn.commit()
            return int(cursor.rowcount or 0) > 0
        finally:
            conn.close()

    def delete_memory(self, memory_id: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM memories WHERE memory_id = ?", (str(memory_id or "").strip(),))
            conn.commit()
            return int(cursor.rowcount or 0) > 0
        finally:
            conn.close()

    def update_memory(
        self,
        memory_id: str,
        *,
        memory_body: str,
        minimal_memory: str | None = None,
        now_iso: str,
    ) -> bool:
        target_id = str(memory_id or "").strip()
        conn = self._connect()
        try:
            if minimal_memory is None:
                cursor = conn.execute(
                    "UPDATE memories SET memory_body = ?, updated_at = ? WHERE memory_id = ?",
                    (str(memory_body or ""), str(now_iso or ""), target_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE memories SET memory_body = ?, minimal_memory = ?, updated_at = ? WHERE memory_id = ?",
                    (str(memory_body or ""), str(minimal_memory or ""), str(now_iso or ""), target_id),
                )
            conn.commit()
            return int(cursor.rowcount or 0) > 0
        finally:
            conn.close()


def _emit_audit_memory_parked(record: dict[str, Any], *, trigger: str = "initial") -> None:
    """尽力而为：记忆批次停车写一条日志审计事件（失败绝不阻塞宿主流程）。

    审计里只读；重试/放弃的操作面仍在记忆板块的失败停车面板。
    """
    try:
        from g3ku.audit_events import emit_audit_event

        category = str(record.get("category") or "").strip()
        reason = str(record.get("discard_reason") or "").strip()
        request_ids = list(record.get("request_ids") or [])
        error_text = str(record.get("last_error_text") or "").strip()
        emit_audit_event(
            "memory",
            "error",
            "memory_batch_parked",
            "记忆批次停车：" + str(reason or category or "未知原因")[:120],
            detail={
                "failed_id": str(record.get("failed_id") or "").strip(),
                "category": category,
                "discard_reason": reason,
                "park_count": int(record.get("park_count", 0) or 0),
                "trigger": str(trigger or "initial").strip(),
                "request_count": len(request_ids),
                "error_text": error_text[:500],
            },
        )
    except Exception:
        pass


class MemoryManager:
    _PROCESSED_BATCH_RETENTION_DAYS = 7

    def __init__(self, workspace: Path, config: Any, *, read_only_init: bool = False):
        self.workspace = Path(workspace)
        self.config = config
        self._read_only_init = bool(read_only_init)
        self.runtime_dir = self.workspace / ".g3ku"
        self.request_artifacts_dir = self.runtime_dir / "memory-requests"
        self.mem_dir = self.workspace / "memory"
        self.memory_file = self.workspace / str(config.document.memory_file)
        self.notes_dir = self.workspace / str(config.document.notes_dir)
        self.queue_file = self.workspace / str(config.queue.queue_file)
        self.ops_file = self.workspace / str(config.queue.ops_file)
        self.failed_file = self.workspace / str(
            getattr(config.queue, "failed_file", "") or "memory/failed.jsonl"
        )
        self.review_state_file = self.mem_dir / "review_state.json"
        self._io_lock = threading.RLock()
        self._processed_request_ids_cache: tuple[tuple[int, int], set[str]] | None = None
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._strategy = MemoryStrategyV2(config)
        if self._read_only_init:
            return
        preserved_document = self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""
        preserved_notes = self._read_note_bodies()
        self._ensure_layout()
        memory_state_db_path = str(getattr(config, "state_db_path", "") or "memory/memory_state.sqlite3")
        self._memory_repo = _MemorySqliteRepository(self.workspace / memory_state_db_path)
        self._restore_preserved_memory_state(
            document_text=preserved_document,
            note_bodies=preserved_notes,
        )
        # sqlite is the only authoritative store, so the mirror is reconciled at boot
        # rather than on the next commit. Reconcile (not a bare repaint) because a
        # non-empty mirror that sqlite does not produce is orphan text: it has to be
        # archived as evidence first, never silently overwritten. A boot must never
        # fail because the store is over the document cap, so this is best-effort.
        try:
            with self._io_lock:
                # Ownership has to be in the store before anything reads it from there:
                # rows written while the marker was the only record backfill into
                # `note_ref`. Best-effort for the same reason as the reconciliation.
                self._memory_repo.backfill_note_refs()
                self._reconcile_mirror_with_sqlite_locked(now_iso=self._now_iso(), at_boot=True)
        except Exception as exc:
            logger.error(f"memory mirror reconciliation failed at boot: {exc}")

    def _ensure_layout(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.request_artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text("", encoding="utf-8")
        else:
            current = self.memory_file.read_text(encoding="utf-8").strip()
            if current == "# Managed Memory Mirror":
                self.memory_file.write_text("", encoding="utf-8")
        if not self.queue_file.exists():
            self.queue_file.write_text("", encoding="utf-8")
        if not self.ops_file.exists():
            self.ops_file.write_text("", encoding="utf-8")
        if not self.failed_file.exists():
            self.failed_file.write_text("", encoding="utf-8")
        if not self.review_state_file.exists():
            self.review_state_file.write_text('{"sessions": {}}\n', encoding="utf-8")

    def _read_note_bodies(self) -> dict[str, str]:
        if not self.notes_dir.exists():
            return {}
        note_bodies: dict[str, str] = {}
        for path in self.notes_dir.glob("*.md"):
            try:
                note_bodies[path.stem] = path.read_text(encoding="utf-8")
            except OSError:
                continue
        return note_bodies

    def _restore_preserved_memory_state(self, *, document_text: str, note_bodies: dict[str, str]) -> None:
        preserved_document = str(document_text or "")
        if preserved_document.strip():
            current_document = self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""
            if not current_document.strip():
                self._atomic_write_text(self.memory_file, preserved_document)
        for ref, body in dict(note_bodies or {}).items():
            note_path = self.notes_dir / note_file_name(ref)
            current_body = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
            if note_path.exists() and current_body.strip():
                continue
            note_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_text(note_path, str(body or ""))

    def start(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="g3ku-memory-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def snapshot_text(self, **_: Any) -> str:
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8").strip()

    def _document_text_from_sqlite_rows(self, rows: list[dict[str, Any]], *, now_iso: str | None = None) -> str:
        entries: list[MemoryEntry] = []
        for row in rows:
            updated_at = str(row.get("updated_at") or now_iso or self._now_iso())
            date_text = self._memory_date_text(updated_at)
            memory_body = str(row.get("memory_body") or "").strip()
            minimal_memory = str(row.get("minimal_memory") or "").strip()
            note_ref = self._extract_note_ref(minimal_memory) or self._extract_note_ref(memory_body)
            summary = minimal_memory if row.get("is_compressed") else memory_body
            summary = self._summary_with_note_ref(summary, note_ref)
            entries.append(
                MemoryEntry(
                    memory_id=str(row["memory_id"]),
                    date_text=date_text,
                    source=str(row["source"]),
                    summary=summary,
                    note_ref=note_ref,
                )
            )
        return self._normalize_document_text("".join(format_memory_entry(entry) for entry in entries))

    def _reconcile_mirror_with_sqlite_locked(self, *, now_iso: str, at_boot: bool = False) -> str:
        """Align the mirror with sqlite. sqlite is the only authoritative store.

        Returns "", "drift_archived", "rebuilt_from_sqlite" or "legacy_mirror_kept".
        The mirror never writes into sqlite: a truncated or hand-edited `MEMORY.md` used
        to be parsed back into the store here, which is how already-compressed rows lost
        their full body (the rendered form of a compressed row *is* its minimal string)
        and how one torn write could wipe the library.

        With `at_boot`, a mirror that yields no managed entries at all is left alone:
        that is the shape of a pre-migration legacy `MEMORY.md`, which `import-legacy`
        and `cleanup-legacy` still have to read. Mid-session (no `at_boot`) the same
        content is archived and repainted, because a commit must not leave a divergent
        projection standing.
        """
        current_snapshot = self._normalize_document_text(self.snapshot_text())
        sqlite_snapshot = self._document_text_from_sqlite_rows(self._memory_repo.list_memories(), now_iso=now_iso)
        if current_snapshot == sqlite_snapshot:
            return ""
        if current_snapshot:
            if at_boot and not parse_memory_document(current_snapshot):
                logger.warning(
                    "memory/MEMORY.md holds no managed memory blocks; treating it as pre-migration "
                    "legacy input and leaving it for `negi memory import-legacy` / `cleanup-legacy`"
                )
                return "legacy_mirror_kept"
            # A non-empty mirror that sqlite does not produce: external edit, pasted
            # text, or a torn write. Keep it as evidence, then repaint it from sqlite.
            self._archive_snapshot_locked()
            self._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)
            return "drift_archived"
        self._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)
        return "rebuilt_from_sqlite"

    def _archive_snapshot_locked(self) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = self.memory_file.with_name(f"{self.memory_file.name}.bak.{stamp}")
        try:
            shutil.copyfile(self.memory_file, target)
        except OSError:
            return self.memory_file
        return target

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """Write through a same-directory temp file, then ``os.replace``.

        Readers must never observe a half-written mirror: ``MEMORY.md`` is injected at
        message index 1, so a truncated document is a torn prompt prefix rather than a
        recoverable read error. The temp file stays beside the target so the replace
        never crosses filesystems.
        """
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _validate_memory_document_text(self, document_text: str) -> None:
        validate_memory_document(
            document_text,
            summary_max_chars=int(getattr(self.config.document, "summary_max_chars", 300) or 300),
            document_max_chars=max(int(getattr(self.config.document, "document_max_chars", 20000) or 20000), 1),
        )

    def _rebuild_memory_snapshot_from_sqlite(self, *, now_iso: str | None = None) -> None:
        document_text = self._document_text_from_sqlite_rows(self._memory_repo.list_memories(), now_iso=now_iso)
        self._validate_memory_document_text(document_text)
        self._atomic_write_text(self.memory_file, document_text)

    async def _append_queue_request(self, request: MemoryQueueRequest) -> None:
        rows = self._read_queue_requests()
        rows.append(self._normalize_queue_request(request))
        self._write_queue_requests(rows)

    async def list_queue(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._read_queue_requests()
        start = max(int(offset or 0), 0)
        end = start + max(int(limit or 0), 0)
        return [asdict(item) for item in rows[start:end]]

    async def list_queue_page(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = self._read_queue_requests()
        start = max(int(offset or 0), 0)
        page_size = max(int(limit or 0), 0)
        end = start + page_size
        return {
            # 只对这一页做投影：单条入队载荷实测最大 400,264 字符，整份给浏览器就是抽屉卡死的来源。
            "items": [self._project_queue_row_for_read(asdict(item)) for item in rows[start:end]],
            "total": len(rows),
            "has_more": end < len(rows),
        }

    async def list_processed_page(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = self._read_processed_batches()
        ordered = list(reversed(rows))
        start = max(int(offset or 0), 0)
        page_size = max(int(limit or 0), 0)
        end = start + page_size
        items = [
            self._project_processed_row_for_read(self._backfill_legacy_changes(row))
            for row in ordered[start:end]
        ]
        return {
            "items": items,
            "total": len(ordered),
            "has_more": end < len(ordered),
        }

    def list_current_memories(self) -> list[dict[str, Any]]:
        """Return every memory row stored in sqlite, with all persisted fields.

        Includes memory_id, memory_body, minimal_memory, source, from_user,
        refresh_count, passed_count, is_compressed, note_ref, consolidated_into,
        created_at, updated_at.
        Used by the read-only "view memories" admin surface.
        """
        repo = getattr(self, "_memory_repo", None)
        if repo is None:
            return []
        return repo.list_memories()

    async def update_current_memory(
        self,
        memory_id: str,
        *,
        memory_body: str,
        minimal_memory: str | None = None,
    ) -> dict[str, Any]:
        """管理员编辑单条记忆正文（sqlite 为源，MEMORY.md 镜像同步重建）。

        先按候选文档做长度/格式校验，再写库与重建镜像，避免超限内容破坏快照。
        """
        target_id = str(memory_id or "").strip()
        if not target_id:
            raise ValueError("memory_id is required")
        new_body = str(memory_body or "").strip()
        if not new_body:
            raise ValueError("memory_body must not be empty")
        new_minimal = None if minimal_memory is None else str(minimal_memory or "").strip()
        if new_minimal is not None and not new_minimal:
            raise ValueError("minimal_memory must not be empty when provided")
        now_iso = self._now_iso()
        with self._io_lock:
            repo = getattr(self, "_memory_repo", None)
            if repo is None:
                raise KeyError(f"memory store unavailable: {target_id}")
            rows = [dict(row) for row in repo.list_memories()]
            target_row = next(
                (row for row in rows if str(row.get("memory_id") or "").strip() == target_id),
                None,
            )
            if target_row is None:
                raise KeyError(f"memory not found: {target_id}")
            candidate_rows = []
            for row in rows:
                candidate = dict(row)
                if str(candidate.get("memory_id") or "").strip() == target_id:
                    candidate["memory_body"] = new_body
                    if new_minimal is not None:
                        candidate["minimal_memory"] = new_minimal
                candidate_rows.append(candidate)
            candidate_text = self._document_text_from_sqlite_rows(candidate_rows, now_iso=now_iso)
            validate_memory_document(
                candidate_text,
                summary_max_chars=int(getattr(self.config.document, "summary_max_chars", 300) or 300),
                document_max_chars=max(int(getattr(self.config.document, "document_max_chars", 20000) or 20000), 1),
            )
            updated = repo.update_memory(
                target_id,
                memory_body=new_body,
                minimal_memory=new_minimal,
                now_iso=now_iso,
            )
            if not updated:
                raise KeyError(f"memory not found: {target_id}")
            self._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)
        return {
            "memory_id": target_id,
            "memory_body": new_body,
            "updated_at": now_iso,
            **({"minimal_memory": new_minimal} if new_minimal is not None else {}),
        }

    async def delete_current_memories(
        self,
        memory_ids: list[str],
        *,
        reason: str = "",
        note_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """管理员批量删除记忆（sqlite 为源，MEMORY.md 镜像同步重建）。

        `note_refs` 为操作员在删除确认弹窗中勾选的关联笔记：仅删除显式勾选的
        note 文件，未勾选的 note 保留（孤儿 note 由 doctor / reconcile-notes 报告）。
        """
        ids: list[str] = []
        for raw_id in list(memory_ids or []):
            normalized = str(raw_id or "").strip()
            if normalized and normalized not in ids:
                ids.append(normalized)
        if not ids:
            raise ValueError("memory_ids is required")
        requested_note_refs: list[str] = []
        for raw_ref in list(note_refs or []):
            normalized_ref = str(raw_ref or "").strip()
            if normalized_ref and normalized_ref not in requested_note_refs:
                requested_note_refs.append(normalized_ref)
        now_iso = self._now_iso()
        with self._io_lock:
            repo = getattr(self, "_memory_repo", None)
            if repo is None:
                raise KeyError("memory store unavailable")
            deleted: list[str] = []
            missing: list[str] = []
            for memory_id in ids:
                if repo.delete_memory(memory_id):
                    deleted.append(memory_id)
                else:
                    missing.append(memory_id)
            if deleted:
                self._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)
            notes_deleted: list[str] = []
            notes_missing: list[str] = []
            for ref in requested_note_refs:
                path = self.notes_dir / note_file_name(ref)
                if path.exists():
                    path.unlink()
                    notes_deleted.append(ref)
                else:
                    notes_missing.append(ref)
        return {
            "deleted": deleted,
            "missing": missing,
            "deleted_at": now_iso,
            "reason": str(reason or "").strip(),
            "notes_deleted": notes_deleted,
            "notes_missing": notes_missing,
        }

    async def update_note(self, ref: str, *, body: str) -> dict[str, Any]:
        """管理员编辑 note 正文（note 预览窗的受控编辑面，无删除能力）。"""
        normalized_ref = str(ref or "").strip()
        if not normalized_ref:
            raise ValueError("note ref is required")
        new_body = str(body or "")
        if not new_body.strip():
            raise ValueError("note body must not be empty")
        with self._io_lock:
            path = self.notes_dir / note_file_name(normalized_ref)
            if not path.exists():
                raise KeyError(f"memory note not found: {normalized_ref}")
            self._atomic_write_text(path, new_body)
        return {
            "ref": normalized_ref,
            "body": new_body,
            "updated_at": self._now_iso(),
        }

    def doctor_report(
        self,
        *,
        now_iso: str | None = None,
        stuck_after_seconds: int = 300,
    ) -> dict[str, Any]:
        snapshot_text = self.snapshot_text()
        summary_limit = int(getattr(self.config.document, "summary_max_chars", 300) or 300)
        document_limit = int(getattr(self.config.document, "document_max_chars", 20000) or 20000)
        document_error = ""
        document_valid = True
        try:
            validate_memory_document(
                snapshot_text,
                summary_max_chars=summary_limit,
                document_max_chars=document_limit,
            )
        except Exception as exc:
            document_valid = False
            document_error = str(exc or "invalid memory document").strip()

        note_refs = self._referenced_note_refs()
        note_files = self._note_file_refs()
        missing_note_refs = sorted(ref for ref in note_refs if ref not in note_files)
        orphan_notes = sorted(ref for ref in note_files if ref not in note_refs)
        queue_rows, queue_parse_errors = self._read_queue_requests_with_diagnostics()
        stuck_processing_head = self._stuck_processing_head(
            queue_rows=queue_rows,
            now_iso=now_iso,
            stuck_after_seconds=stuck_after_seconds,
        )
        parked_records = [
            record
            for record in self._read_failed_records()
            if str(record.get("status") or "").strip() == "parked"
        ]
        checks = [
            {
                "name": "memory_document",
                "ok": document_valid,
                "detail": "valid"
                if document_valid
                else (document_error or "memory document contains invalid blocks"),
            },
            {
                "name": "missing_note_refs",
                "ok": not missing_note_refs,
                "detail": "none" if not missing_note_refs else ", ".join(missing_note_refs),
            },
            {
                "name": "orphan_notes",
                "ok": not orphan_notes,
                "detail": "none" if not orphan_notes else ", ".join(orphan_notes),
            },
            {
                "name": "queue_parse_errors",
                "ok": not queue_parse_errors,
                "detail": "none"
                if not queue_parse_errors
                else ", ".join(
                    f"line {int(item.get('line', 0) or 0)}: {str(item.get('error') or 'unknown parse error')}"
                    for item in queue_parse_errors
                ),
            },
            {
                "name": "stuck_processing_head",
                "ok": stuck_processing_head is None,
                "detail": "none"
                if stuck_processing_head is None
                else f"{stuck_processing_head['request_id']} age={stuck_processing_head['age_seconds']}s",
            },
            {
                "name": "failed_parked",
                "ok": not parked_records,
                "detail": "none"
                if not parked_records
                else ", ".join(
                    f"{str(record.get('failed_id') or '')}({str(record.get('category') or '')})"
                    for record in parked_records[:5]
                )
                + (f" +{len(parked_records) - 5} more" if len(parked_records) > 5 else ""),
            },
        ]
        return {
            "ok": all(bool(item["ok"]) for item in checks),
            "status": "ok" if all(bool(item["ok"]) for item in checks) else "issues_found",
            "checks": checks,
            "memory_document_valid": document_valid,
            "memory_document_error": document_error,
            "memory_note_refs": note_refs,
            "missing_note_refs": missing_note_refs,
            "orphan_notes": orphan_notes,
            "queue_depth": len(queue_rows),
            "queue_head": asdict(queue_rows[0]) if queue_rows else None,
            "queue_parse_errors": queue_parse_errors,
            "stuck_processing_head": stuck_processing_head,
            "failed_parked_count": len(parked_records),
        }

    def reconcile_notes(self, *, delete_orphans: bool = False) -> dict[str, Any]:
        note_refs = self._referenced_note_refs()
        note_files = self._note_file_refs()
        missing_note_refs = sorted(ref for ref in note_refs if ref not in note_files)
        orphan_notes = sorted(ref for ref in note_files if ref not in note_refs)
        created_notes: list[str] = []
        deleted_notes: list[str] = []

        for ref in missing_note_refs:
            note_path = self.notes_dir / note_file_name(ref)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_text(
                note_path,
                self._normalize_note_text(
                    f"Placeholder note created by reconcile-notes for ref:{ref}."
                ),
            )
            created_notes.append(ref)

        if delete_orphans:
            for ref in orphan_notes:
                note_path = self.notes_dir / note_file_name(ref)
                if not note_path.exists():
                    continue
                try:
                    note_path.unlink()
                except OSError:
                    continue
                deleted_notes.append(ref)

        return {
            "ok": True,
            "status": "reconciled",
            "memory_note_refs": note_refs,
            "created_missing_notes": created_notes,
            "created_missing_count": len(created_notes),
            "orphan_notes_detected": orphan_notes,
            "deleted_orphan_notes": deleted_notes,
            "deleted_orphan_count": len(deleted_notes),
            "delete_orphans": bool(delete_orphans),
        }

    def import_legacy(
        self,
        legacy_path: Path | str,
        *,
        apply: bool = False,
    ) -> dict[str, Any]:
        path = Path(legacy_path)
        rows = self._load_legacy_rows(path)
        document_text, note_writes = self._build_legacy_import_payload(rows)
        note_refs = self._collect_note_refs(document_text)
        report = {
            "ok": True,
            "status": "dry_run" if not apply else "applied",
            "legacy_path": str(path),
            "entry_count": len(parse_memory_document(document_text)),
            "note_ref_count": len(note_refs),
            "note_refs": note_refs,
            "document_preview": self._document_preview(document_text),
            "apply": bool(apply),
        }
        if not apply:
            return report

        if self.snapshot_text().strip():
            raise ValueError("memory/MEMORY.md is non-empty; import-legacy only applies to an empty target")
        if self._read_queue_requests():
            raise ValueError("memory queue is not empty; flush or reconcile the queue before import-legacy --apply")

        validated = _MemoryValidatedWrite(
            document_text=document_text,
            note_writes={ref: self._normalize_note_text(body) for ref, body in note_writes.items()},
            note_refs_written=sorted(note_writes.keys()),
            memory_chars_after=len(document_text),
            document_preview=self._document_preview(document_text),
            adopt_document=True,
        )
        self._commit_validated_write(validated)
        report["note_refs_written"] = list(validated.note_refs_written)
        return report

    def legacy_cleanup_report(self, *, apply: bool = False) -> dict[str, Any]:
        existing_paths: list[str] = []
        data_bearing_paths: list[str] = []
        resolved_paths: list[Path] = []

        for relative_path in _LEGACY_CLEANUP_RELATIVE_PATHS:
            path = self.workspace / relative_path
            if not path.exists():
                continue
            existing_paths.append(relative_path)
            resolved_paths.append(path)
            if path.is_dir():
                try:
                    has_children = any(path.iterdir())
                except OSError:
                    has_children = True
                if has_children:
                    data_bearing_paths.append(relative_path)
                continue
            try:
                if path.read_text(encoding="utf-8").strip():
                    data_bearing_paths.append(relative_path)
            except OSError:
                data_bearing_paths.append(relative_path)

        report = {
            "ok": True,
            "status": "dry_run" if not apply else "applied",
            "apply": bool(apply),
            "existing_paths": existing_paths,
            "data_bearing_paths": data_bearing_paths,
            "deleted_paths": [],
            "queue_depth": len(self._read_queue_requests()),
            "memory_document_empty": not bool(self.snapshot_text().strip()),
        }
        if not apply:
            return report

        if self._read_queue_requests():
            raise ValueError("memory queue is not empty; flush or reconcile the queue before cleanup-legacy --apply")
        if not self.snapshot_text().strip() and data_bearing_paths:
            raise ValueError(
                "legacy memory artifacts still contain data while memory/MEMORY.md is empty; "
                "run import-legacy first or delete them only after manual review"
            )

        deleted_paths: list[str] = []
        for relative_path, path in zip(existing_paths, resolved_paths):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                continue
            deleted_paths.append(relative_path)
        report["deleted_paths"] = deleted_paths
        return report

    async def collect_due_batch(self, *, now_iso: str) -> MemoryBatch | None:
        rows = self._read_queue_requests()
        if not rows:
            return None
        head = rows[0]
        if str(head.status or "pending").strip() == "processing":
            retry_after = self._parse_optional_iso(head.retry_after)
            if retry_after is not None and retry_after > datetime.fromisoformat(str(now_iso).strip()):
                return None
            items: list[MemoryQueueRequest] = []
            for row in rows:
                if str(row.status or "").strip() != "processing" or row.op != head.op:
                    break
                items.append(row)
            return MemoryBatch(op=head.op, items=items) if items else None

        max_chars = int(getattr(self.config.queue, "batch_max_chars", 50000) or 50000)
        current_chars = 0
        hit_char_boundary = False
        items: list[MemoryQueueRequest] = []
        for row in rows:
            if str(row.status or "pending").strip() != "pending":
                break
            if row.op != head.op:
                break
            next_chars = current_chars + len(str(row.payload_text or ""))
            if items and next_chars > max_chars:
                hit_char_boundary = True
                break
            current_chars = next_chars
            items.append(row)

        if not items:
            return None
        waited_seconds = self._seconds_since(head.created_at, now_iso)
        if (
            not hit_char_boundary
            and current_chars < max_chars
            and waited_seconds < int(getattr(self.config.queue, "max_wait_seconds", 3) or 3)
        ):
            return None
        return MemoryBatch(op=head.op, items=items)

    def load_note(self, ref: str) -> str:
        path = self.notes_dir / note_file_name(ref)
        if not path.exists():
            raise FileNotFoundError(f"memory note not found: {ref}")
        return path.read_text(encoding="utf-8")

    def close(self) -> None:
        self._stop_event.set()
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        self._worker_thread = None

    def _memory_worker_lock_path(self) -> Path:
        return self.mem_dir / ".worker.lock"

    def _memory_worker_lock_metadata(self) -> dict[str, object]:
        return {
            "pid": int(os.getpid()),
            "thread": str(threading.current_thread().name or "").strip() or "unknown",
            "workspace": str(self.workspace),
            "kind": "memory_worker",
        }

    def _try_acquire_memory_worker_lease(self) -> Any | None:
        return _try_acquire_file_lock(
            self._memory_worker_lock_path(),
            metadata=self._memory_worker_lock_metadata(),
        )

    @staticmethod
    def _request_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _parse_optional_iso(value: str | None) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    @classmethod
    def _retry_after_iso(cls, now_iso: str, seconds: int) -> str:
        now = datetime.fromisoformat(str(now_iso or "").strip())
        return (now + timedelta(seconds=max(int(seconds or 0), 0))).isoformat()

    @staticmethod
    def _seconds_since(start_iso: str, end_iso: str) -> int:
        start = datetime.fromisoformat(str(start_iso or "").strip())
        end = datetime.fromisoformat(str(end_iso or "").strip())
        return max(0, int((end - start).total_seconds()))

    @staticmethod
    def _normalize_decision_source(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return "user" if normalized == "user" else "self"

    def classify_memory_payload(
        self,
        *,
        decision_source: str,
        payload_text: str,
        trigger_source: str = "",
        op: str = "write",
    ) -> MemoryPayloadClassification:
        return self._strategy.classify_memory_payload(
            decision_source=decision_source,
            payload_text=payload_text,
            trigger_source=trigger_source,
            op=op,
        )

    def should_enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
    ) -> AutonomousReviewDecision:
        return self._strategy.should_enqueue_autonomous_review(
            session_key=session_key,
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
        )

    def prioritize_existing_entries(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        return self._strategy.prioritize_existing_entries(entries)

    def _worker_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                asyncio.run(self.run_due_batch_once())
            except Exception:
                continue

    def _normalize_queue_request(self, request: MemoryQueueRequest) -> MemoryQueueRequest:
        return MemoryQueueRequest(
            op=str(request.op or "").strip(),
            decision_source=self._normalize_decision_source(request.decision_source),
            payload_text=str(request.payload_text or "").strip(),
            created_at=str(request.created_at or "").strip() or self._now_iso(),
            request_id=str(request.request_id or "").strip() or self._request_id(str(request.op or "memory")),
            trigger_source=str(request.trigger_source or "").strip(),
            session_key=str(request.session_key or "").strip(),
            status=str(request.status or "pending").strip() or "pending",
            processing_started_at=str(request.processing_started_at or "").strip(),
            last_error_text=str(request.last_error_text or "").strip(),
            last_error_at=str(request.last_error_at or "").strip(),
            retry_after=str(request.retry_after or "").strip(),
        )

    def _read_queue_requests(self) -> list[MemoryQueueRequest]:
        if not self.queue_file.exists():
            return []
        with self._io_lock:
            return [
                self._normalize_queue_request(MemoryQueueRequest(**json.loads(line)))
                for line in self.queue_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def _read_queue_requests_with_diagnostics(self) -> tuple[list[MemoryQueueRequest], list[dict[str, Any]]]:
        if not self.queue_file.exists():
            return [], []
        with self._io_lock:
            lines = self.queue_file.read_text(encoding="utf-8").splitlines()
        rows: list[MemoryQueueRequest] = []
        parse_errors: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            if not str(line or "").strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("queue row must be a JSON object")
                rows.append(self._normalize_queue_request(MemoryQueueRequest(**payload)))
            except Exception as exc:
                error_text = str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__
                parse_errors.append(
                    {
                        "line": line_number,
                        "error": error_text,
                    }
                )
        return rows, parse_errors

    def _write_queue_requests(self, rows: list[MemoryQueueRequest]) -> None:
        with self._io_lock:
            text = "\n".join(json.dumps(asdict(item), ensure_ascii=False) for item in rows)
            self._atomic_write_text(self.queue_file, (f"{text}\n" if text else ""))

    def _write_processed_batches(self, rows: list[dict[str, Any]]) -> None:
        with self._io_lock:
            text = "\n".join(json.dumps(item, ensure_ascii=False) for item in rows)
            self._atomic_write_text(self.ops_file, (f"{text}\n" if text else ""))

    def _read_processed_batches(self, *, prune_expired: bool = True, now_iso: str | None = None) -> list[dict[str, Any]]:
        if not self.ops_file.exists():
            return []
        with self._io_lock:
            rows = [
                json.loads(line)
                for line in self.ops_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if not prune_expired:
            return rows
        kept_rows, changed = self._prune_processed_batches(rows, now_iso=now_iso)
        if changed and not self._read_only_init:
            self._write_processed_batches(kept_rows)
        return kept_rows

    def _processed_batches_file_key(self) -> tuple[int, int] | None:
        try:
            stat = self.ops_file.stat()
        except OSError:
            return None
        return (int(stat.st_size), int(stat.st_mtime_ns))

    def _processed_request_ids(self) -> set[str]:
        """已处理批次的 request_id 集合，按台账文件的 (size, mtime_ns) 缓存。

        `ops.jsonl` 每行连批次正文一起存，实盘 20 行就是 1.69 MB，而这个集合每次
        due-batch 节拍都要过一遍——全量读+解析因此成了循环上的头几名。失效判据只用
        文件身份就够：写入走 `_write_processed_batches`（同进程、同 `_io_lock`），
        size 与 mtime 必变；跨进程追加同样会变。剪枝窗口按天计（
        `_PROCESSED_BATCH_RETENTION_DAYS`），所以"文件没动就不重读"不会让任何一条
        过期判定迟到到有意义的粒度。返回副本，调用方改集合不会污染缓存。
        """
        key = self._processed_batches_file_key()
        cached = self._processed_request_ids_cache
        if key is not None and cached is not None and cached[0] == key:
            return set(cached[1])
        request_ids: set[str] = set()
        for payload in self._read_processed_batches():
            if not isinstance(payload, dict):
                continue
            for raw_request_id in list(payload.get("request_ids") or []):
                normalized = str(raw_request_id or "").strip()
                if normalized:
                    request_ids.add(normalized)
        if key is not None:
            self._processed_request_ids_cache = (key, request_ids)
        return set(request_ids)

    def _drop_processed_queue_requests(self, *, processed_request_ids: set[str] | None = None) -> list[str]:
        known_processed = set(processed_request_ids or set())
        if not known_processed:
            known_processed = self._processed_request_ids()
        if not known_processed:
            return []

        rows = self._read_queue_requests()
        remaining: list[MemoryQueueRequest] = []
        dropped: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row.request_id in known_processed:
                if row.request_id not in seen:
                    dropped.append(row.request_id)
                    seen.add(row.request_id)
                continue
            remaining.append(row)
        if dropped:
            self._write_queue_requests(remaining)
        return dropped

    def _append_ops_payload(self, payload: dict[str, Any]) -> None:
        rows = self._read_processed_batches()
        rows.append(dict(payload or {}))
        self._write_processed_batches(rows)

    def _note_file_refs(self) -> set[str]:
        return {path.stem for path in self.notes_dir.glob("*.md")}

    def _stuck_processing_head(
        self,
        *,
        queue_rows: list[MemoryQueueRequest],
        now_iso: str | None,
        stuck_after_seconds: int,
    ) -> dict[str, Any] | None:
        if not queue_rows:
            return None
        head = queue_rows[0]
        if str(head.status or "").strip() != "processing":
            return None
        effective_now = self._parse_optional_iso(now_iso) or datetime.now().astimezone()
        retry_after = self._parse_optional_iso(head.retry_after)
        if retry_after is not None and retry_after > effective_now:
            return None
        started_at = self._parse_optional_iso(head.processing_started_at) or self._parse_optional_iso(head.created_at)
        if started_at is None:
            return {
                "request_id": head.request_id,
                "op": head.op,
                "age_seconds": None,
                "processing_started_at": head.processing_started_at,
                "retry_after": head.retry_after,
                "last_error_text": head.last_error_text,
            }
        age_seconds = max(0, int((effective_now - started_at).total_seconds()))
        if age_seconds < max(int(stuck_after_seconds or 0), 0):
            return None
        return {
            "request_id": head.request_id,
            "op": head.op,
            "age_seconds": age_seconds,
            "processing_started_at": head.processing_started_at,
            "retry_after": head.retry_after,
            "last_error_text": head.last_error_text,
        }

    def _load_legacy_rows(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"legacy import file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("legacy import file is empty")
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict) and isinstance(payload.get("entries"), list):
                rows = payload["entries"]
            else:
                raise ValueError("legacy import payload must be a JSON array, JSONL file, or an object with an entries list")
        normalized = [dict(item) for item in rows if isinstance(item, dict)]
        if not normalized:
            raise ValueError("legacy import payload does not contain any object entries")
        return normalized

    def _build_legacy_import_payload(self, rows: list[dict[str, Any]]) -> tuple[str, dict[str, str]]:
        entries: list[MemoryEntry] = []
        note_writes: dict[str, str] = {}
        allocated_ids: set[str] = set()
        for index, row in enumerate(rows, start=1):
            date_text = str(row.get("date_text") or "").strip()
            source = str(row.get("source") or "").strip().lower()
            summary = str(row.get("summary") or "").strip()
            note_ref = str(row.get("note_ref") or "").strip()
            note_text = str(row.get("note_text") or "").strip()
            if not date_text or not source or not summary:
                raise ValueError(f"legacy import row {index} is missing date_text, source, or summary")
            if source not in {"user", "self"}:
                raise ValueError(f"legacy import row {index} has unsupported source: {source}")
            if note_ref and f"ref:{note_ref}" not in summary:
                summary = f"{summary} ref:{note_ref}"
            memory_id = self._generate_memory_id(allocated_ids)
            allocated_ids.add(memory_id)
            entries.append(
                MemoryEntry(
                    memory_id=memory_id,
                    date_text=date_text,
                    source=source,
                    summary=summary,
                    note_ref=note_ref,
                )
            )
            if note_ref:
                if not note_text and not (self.notes_dir / note_file_name(note_ref)).exists():
                    raise ValueError(f"legacy import row {index} references {note_ref} without note_text")
                if note_text:
                    note_writes[note_ref] = note_text

        document_text = "".join(format_memory_entry(entry) for entry in entries)
        validate_memory_document(
            document_text,
            summary_max_chars=int(getattr(self.config.document, "summary_max_chars", 300) or 300),
            document_max_chars=int(getattr(self.config.document, "document_max_chars", 20000) or 20000),
        )
        note_refs = self._collect_note_refs(document_text)
        missing_refs = sorted(ref for ref in note_refs if ref not in note_writes and ref not in self._note_file_refs())
        if missing_refs:
            raise ValueError(f"legacy import payload is missing note bodies for refs: {', '.join(missing_refs)}")
        return document_text, note_writes

    def _mark_batch_processing(self, items: list[MemoryQueueRequest], now_iso: str) -> None:
        request_ids = {item.request_id for item in items}
        rows = self._read_queue_requests()
        updated: list[MemoryQueueRequest] = []
        for row in rows:
            if row.request_id in request_ids:
                updated.append(
                    row.__class__(
                        **{
                            **asdict(row),
                            "status": "processing",
                            "processing_started_at": row.processing_started_at or now_iso,
                            "last_error_text": "",
                            "last_error_at": "",
                            "retry_after": "",
                        }
                    )
                )
            else:
                updated.append(row)
        self._write_queue_requests(updated)

    def _mark_batch_error(self, batch: MemoryBatch, now_iso: str, error_text: str) -> dict[str, Any]:
        request_ids = {item.request_id for item in batch.items}
        rows = self._read_queue_requests()
        updated: list[MemoryQueueRequest] = []
        retry_after = self._retry_after_iso(now_iso, 30)
        for row in rows:
            if row.request_id in request_ids:
                updated.append(
                    row.__class__(
                        **{
                            **asdict(row),
                            "status": "processing",
                            "processing_started_at": row.processing_started_at or now_iso,
                            "last_error_text": str(error_text or "").strip(),
                            "last_error_at": now_iso,
                            "retry_after": retry_after,
                        }
                    )
                )
            else:
                updated.append(row)
        self._write_queue_requests(updated)
        return {
            "ok": False,
            "status": "error",
            "op": batch.op,
            "processed": 0,
            "request_ids": [item.request_id for item in batch.items],
            "error": str(error_text or "").strip(),
            "retry_after": retry_after,
        }

    def _mark_batch_blocked(self, batch: MemoryBatch, now_iso: str, error_text: str) -> dict[str, Any]:
        payload = self._mark_batch_error(batch, now_iso, error_text)
        payload["status"] = "blocked"
        return payload

    def _drop_request_ids(self, request_ids: set[str]) -> None:
        remaining = [row for row in self._read_queue_requests() if row.request_id not in request_ids]
        self._write_queue_requests(remaining)

    def _memory_model_chain(self, runtime_config: Any) -> list[str]:
        try:
            return [str(item or "").strip() for item in runtime_config.get_role_model_keys("memory") if str(item or "").strip()]
        except Exception:
            return []

    async def _run_memory_agent_attempt(
        self,
        *,
        batch: MemoryBatch,
        runtime_config: Any,
        before_text: str,
        repair_reason: str,
        queue_request_ids: list[str],
        model_chain: list[str],
    ) -> _MemoryAttemptResult:
        session = _MemoryToolSession(
            snapshot_text=before_text,
            notes_dir=self.notes_dir,
            batch_op=str(batch.op or "").strip().lower(),
        )
        tools = self._memory_agent_tools(session)
        tool_schemas = [item.openai_schema() for item in list(tools or [])]
        model = build_chat_model(runtime_config, role="memory")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._memory_agent_system_prompt()},
            {
                "role": "user",
                "content": self._memory_agent_user_prompt(
                    batch=batch,
                    snapshot_text=before_text,
                    repair_reason=repair_reason,
                ),
            },
        ]
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        request_artifacts: list[dict[str, Any]] = []
        round_limit = int(runtime_config.get_role_max_iterations("memory") or 8)
        round_limit = max(round_limit, 1)
        final_text = ""
        for _ in range(round_limit):
            request_messages = self._memory_request_messages(messages)
            self._memory_window_guard(
                runtime_config=runtime_config,
                model_chain=list(model_chain or []),
                request_messages=request_messages,
                tool_schemas=tool_schemas,
                usage_total=usage_total,
                request_artifacts=request_artifacts,
            )
            response = await model.chat(request_messages, tools=tool_schemas or None)
            self._merge_usage(usage_total, self._extract_usage(response))
            artifact = self._persist_memory_request_artifact(
                phase="agent",
                batch_op=batch.op,
                queue_request_ids=queue_request_ids,
                model_chain=model_chain,
                request_messages=request_messages,
                response=response,
            )
            if artifact is not None:
                request_artifacts.append(artifact)
            # Provider 错误响应（429/超时/上游错误被 provider 层转为 finish_reason="error"
            # 的正常返回值）必须在这里显式识别：否则它会伪装成"模型没调工具"的协议违规，
            # 真实根因被校验错误掩盖，usage/工件也全为空。
            provider_error_text = self._provider_error_text(response)
            if provider_error_text:
                raise _MemoryProviderError(
                    provider_error_text,
                    usage=usage_total,
                    request_artifacts=request_artifacts,
                )
            final_text = self._response_text(response)
            tool_calls = self._normalize_tool_calls(response)
            if not tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": final_text,
                    # provider 要求 arguments 是 JSON 字符串；这一跳以前由适配壳完成，
                    # 直连后由落盘侧自己规范成 OpenAI 形状（与 worker 车道同一做法）。
                    "tool_calls": [
                        {
                            "id": call.get("id"),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name") or ""),
                                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for tool_call in tool_calls:
                result = await self._execute_memory_tool(tools, tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(result, ensure_ascii=False),
                        "tool_call_id": str(tool_call.get("id") or ""),
                        "name": str(tool_call.get("name") or ""),
                    }
                )
        return _MemoryAttemptResult(
            session=session,
            usage=usage_total,
            final_text=final_text,
            request_artifacts=request_artifacts,
        )

    def _memory_agent_system_prompt(self) -> str:
        return load_prompt("memory_agent.md").strip()

    @staticmethod
    def _response_text(response: Any) -> str:
        content = getattr(response, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(item) for item in content)
        return str(content or "")

    @staticmethod
    def _normalize_tool_calls(response: Any) -> list[dict[str, Any]]:
        raw_calls = list(getattr(response, "tool_calls", None) or [])
        normalized: list[dict[str, Any]] = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                if getattr(raw_call, "name", None) is None:
                    continue
                raw_call = {
                    "name": getattr(raw_call, "name", ""),
                    "args": getattr(raw_call, "arguments", None),
                    "id": getattr(raw_call, "id", None),
                }
            name = str(raw_call.get("name") or "").strip()
            if not name and isinstance(raw_call.get("function"), dict):
                name = str(raw_call["function"].get("name") or "").strip()
            raw_args = raw_call.get("args")
            if raw_args is None and "arguments" in raw_call:
                raw_args = raw_call.get("arguments")
            if raw_args is None and isinstance(raw_call.get("function"), dict):
                raw_args = raw_call["function"].get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {"value": raw_args}
            if not isinstance(raw_args, dict):
                raw_args = {}
            normalized.append(
                {
                    "id": str(raw_call.get("id") or f"memory-call-{index + 1}"),
                    "name": name,
                    "args": raw_args,
                }
            )
        return normalized

    @staticmethod
    def _provider_error_text(response: Any) -> str:
        """从模型响应中识别 provider 层错误（错误响应被当作正常返回值传回）。

        provider 失败（429、超时、上游 5xx）会以 finish_reason="error" 的响应形态
        到达这里，而不是抛异常；不显式识别就会伪装成"模型没调工具"的协议违规。
        """
        direct_error_text = str(getattr(response, "error_text", "") or "").strip()
        if direct_error_text:
            return direct_error_text
        if str(getattr(response, "finish_reason", "") or "").strip().lower() == "error":
            content_text = str(getattr(response, "content", "") or "").strip()
            return content_text or "provider returned an error response"
        metadata = getattr(response, "response_metadata", None)
        if not isinstance(metadata, dict):
            return ""
        error_text = str(metadata.get("error_text") or "").strip()
        if error_text:
            return error_text
        finish_reason = str(metadata.get("finish_reason") or "").strip().lower()
        if finish_reason == "error":
            content_text = str(getattr(response, "content", "") or "").strip()
            return content_text or "provider returned an error response"
        return ""

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int]:
        total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        candidates = [
            getattr(response, "usage_metadata", None),
            getattr(response, "usage", None),
            (getattr(response, "response_metadata", None) or {}).get("token_usage")
            if isinstance(getattr(response, "response_metadata", None), dict)
            else None,
            (getattr(response, "response_metadata", None) or {}).get("usage")
            if isinstance(getattr(response, "response_metadata", None), dict)
            else None,
        ]
        for candidate in candidates:
            normalized = normalize_usage_payload(candidate)
            total["input_tokens"] = max(total["input_tokens"], int(normalized.get("input_tokens", 0) or 0))
            total["output_tokens"] = max(total["output_tokens"], int(normalized.get("output_tokens", 0) or 0))
            total["cache_read_tokens"] = max(total["cache_read_tokens"], int(normalized.get("cache_hit_tokens", 0) or 0))
        return total

    @staticmethod
    def _merge_usage(target: dict[str, int], delta: dict[str, int]) -> None:
        for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
            target[key] = int(target.get(key, 0) or 0) + int(delta.get(key, 0) or 0)

    @staticmethod
    def _normalize_document_text(text: str) -> str:
        stripped = str(text or "").strip()
        return f"{stripped}\n" if stripped else ""

    @staticmethod
    def _normalize_note_text(text: str) -> str:
        stripped = str(text or "").strip()
        return f"{stripped}\n" if stripped else ""

    @staticmethod
    def _document_preview(document_text: str) -> str:
        text = str(document_text or "").strip()
        if len(text) <= 240:
            return text
        return text[:237].rstrip() + "..."

    @staticmethod
    def _inline_preview_text(text: str, *, max_chars: int = 240) -> str:
        value = " ".join(str(text or "").split())
        limit = max(int(max_chars or 0), 0)
        if not value or not limit:
            return ""
        if len(value) <= limit:
            return value
        if limit <= 3:
            return value[:limit]
        return value[: limit - 3].rstrip() + "..."

    def _build_change_preview(self, validated: _MemoryValidatedWrite | None) -> str:
        if validated is None:
            return ""
        if str(validated.noop_reason or "").strip():
            return ""

        parts: list[str] = []
        for item in list(validated.adds or []):
            content = self._inline_preview_text(item.get("content") or "")
            if content:
                parts.append(f"新增：{content}")
        for item in list(validated.rewrites or []):
            memory_id = str(item.get("id") or "").strip()
            content = self._inline_preview_text(item.get("content") or "")
            if memory_id and content:
                parts.append(f"修改 {memory_id}：{content}")
            elif content:
                parts.append(f"修改：{content}")
        for memory_id in list(validated.deletes or []):
            normalized_id = str(memory_id or "").strip()
            if normalized_id:
                parts.append(f"删除 {normalized_id}")
        for ref, body in sorted(dict(validated.note_upserts or {}).items()):
            normalized_ref = str(ref or "").strip()
            body_preview = self._inline_preview_text(body or "")
            if normalized_ref and body_preview:
                parts.append(f"更新 note ref:{normalized_ref}：{body_preview}")
            elif normalized_ref:
                parts.append(f"更新 note ref:{normalized_ref}")
        if not parts:
            return ""
        return self._inline_preview_text("；".join(parts), max_chars=400)

    def _build_structured_changes(self, validated: _MemoryValidatedWrite | None) -> list[dict[str, Any]]:
        """Build a full, untruncated, per-memory description of every change in a batch.

        Each entry describes a single memory mutation so the frontend can render one
        block per memory and, for rewrites, show the original vs modified text.
        """
        if validated is None:
            return []
        if str(validated.noop_reason or "").strip():
            return []

        original_bodies = dict(validated.original_bodies or {})
        changes: list[dict[str, Any]] = []
        for item in list(validated.adds or []):
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            changes.append(
                {
                    "type": "add",
                    "memory_id": str(item.get("id") or item.get("memory_id") or "").strip(),
                    "content": str(item.get("content") or ""),
                }
            )
        for item in list(validated.rewrites or []):
            memory_id = str(item.get("id") or "").strip()
            content = str(item.get("content") or "").strip()
            if not content and not memory_id:
                continue
            changes.append(
                {
                    "type": "rewrite",
                    "memory_id": memory_id,
                    "original_content": original_bodies.get(memory_id, ""),
                    "content": str(item.get("content") or ""),
                }
            )
        for memory_id in list(validated.deletes or []):
            normalized_id = str(memory_id or "").strip()
            if not normalized_id:
                continue
            changes.append(
                {
                    "type": "delete",
                    "memory_id": normalized_id,
                    "original_content": original_bodies.get(normalized_id, ""),
                }
            )
        for ref, body in sorted(dict(validated.note_upserts or {}).items()):
            normalized_ref = str(ref or "").strip()
            if not normalized_ref:
                continue
            changes.append(
                {
                    "type": "note_upsert",
                    "note_ref": normalized_ref,
                    "content": str(body or ""),
                }
            )
        for item in list(validated.reaped_notes or []):
            normalized_ref = str(item.get("note_ref") or "").strip()
            if not normalized_ref:
                continue
            changes.append(
                {
                    "type": "note_reaped",
                    "note_ref": normalized_ref,
                    "reason": str(item.get("reason") or "unreferenced").strip(),
                }
            )
        return changes

    # Legacy processed rows kept only a flattened `新增：…；修改 ID：…；删除 ID；更新 note ref:…`
    # preview. A new segment starts after a `；` that is followed by one of those markers.
    _LEGACY_PREVIEW_SEGMENT_SPLIT = re.compile(r"；(?=(?:新增：|修改[ ：]|删除 |更新 note ref:))")

    @classmethod
    def _parse_legacy_change_preview(cls, preview: str) -> list[dict[str, Any]]:
        """Best-effort reconstruction of structured changes from a legacy change_preview.

        Rows committed before the structured `changes` payload carry only the joined,
        whitespace-flattened, 400-char preview text. Split it back into per-memory
        entries so the operator UI renders them in the same block layout. Original
        bodies of rewrites/deletes were never captured for those rows, so the entries
        are flagged with `original_missing` instead of inventing a before-text.
        """
        text = str(preview or "").strip()
        if not text:
            return []
        changes: list[dict[str, Any]] = []
        for segment in cls._LEGACY_PREVIEW_SEGMENT_SPLIT.split(text):
            segment = segment.strip()
            if not segment:
                continue
            if segment.startswith("新增："):
                content = segment[len("新增："):].strip()
                if content:
                    changes.append({"type": "add", "memory_id": "", "content": content})
                continue
            if segment.startswith("修改："):
                content = segment[len("修改："):].strip()
                if content:
                    changes.append(
                        {
                            "type": "rewrite",
                            "memory_id": "",
                            "original_content": "",
                            "original_missing": True,
                            "content": content,
                        }
                    )
                continue
            if segment.startswith("修改 "):
                head, _, content = segment.partition("：")
                memory_id = head[len("修改 "):].strip()
                content = content.strip()
                if memory_id or content:
                    changes.append(
                        {
                            "type": "rewrite",
                            "memory_id": memory_id,
                            "original_content": "",
                            "original_missing": True,
                            "content": content,
                        }
                    )
                continue
            if segment.startswith("删除 "):
                memory_id = segment[len("删除 "):].strip().rstrip("；").strip()
                if memory_id:
                    changes.append(
                        {
                            "type": "delete",
                            "memory_id": memory_id,
                            "original_content": "",
                            "original_missing": True,
                        }
                    )
                continue
            if segment.startswith("更新 note ref:"):
                head, separator, content = segment.partition("：")
                note_ref = head[len("更新 note ref:"):].strip()
                content = content.strip() if separator else ""
                if note_ref:
                    changes.append(
                        {
                            "type": "note_upsert",
                            "note_ref": note_ref,
                            "content": content,
                        }
                    )
                continue
        return changes

    def _backfill_legacy_changes(self, row: dict[str, Any]) -> dict[str, Any]:
        """Attach reconstructed `changes` to a legacy processed row for display.

        Read-only enrichment: the persisted row is never rewritten, and rows that
        already carry structured `changes` (or have no parsable preview) pass through
        unchanged. `changes_reconstructed` marks rows whose blocks were rebuilt from
        the lossy preview rather than recorded at commit time.
        """
        if not isinstance(row, dict) or row.get("changes"):
            return row
        changes = self._parse_legacy_change_preview(row.get("change_preview"))
        if not changes:
            return row
        enriched = dict(row)
        enriched["changes"] = changes
        enriched["changes_reconstructed"] = True
        return enriched

    def _commit_validated_write(self, validated: _MemoryValidatedWrite) -> None:
        now_iso = self._now_iso()
        with self._io_lock:
            # Note bodies land before the store mutation. If the mutation is later
            # rejected, an orphan note file is only surplus bytes - the next successful
            # commit's orphan sweep removes them; the store itself is rolled back whole.
            for ref, body in validated.note_writes.items():
                note_path = self.notes_dir / note_file_name(ref)
                note_path.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write_text(note_path, body)
            self._reconcile_mirror_with_sqlite_locked(now_iso=now_iso)
            # Capture the original bodies of rewritten/deleted memories before the
            # mutation is applied so the processed record can show before/after text.
            if not validated.original_bodies:
                affected_ids = [str(item.get("id") or "").strip() for item in list(validated.rewrites or [])]
                affected_ids += [str(item or "").strip() for item in list(validated.deletes or [])]
                validated.original_bodies = self._memory_repo.fetch_bodies(affected_ids)
            has_structured_mutation = bool(
                list(validated.adds or [])
                or list(validated.rewrites or [])
                or list(validated.deletes or [])
                or list(validated.inspired_memory_ids or [])
            )
            rows_before = self._memory_repo.list_memories()
            try:
                if has_structured_mutation:
                    self._memory_repo.apply_mutation(
                        adds=validated.adds,
                        rewrites=validated.rewrites,
                        deletes=validated.deletes,
                        inspired_memory_ids=validated.inspired_memory_ids,
                        now_iso=now_iso,
                    )
                elif validated.adopt_document:
                    # The only surviving document -> sqlite direction: an explicit
                    # operator legacy import, never an ordinary commit.
                    self._memory_repo.replace_all_from_document(
                        document_text=validated.document_text,
                        now_iso=now_iso,
                    )
                self._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)
            except Exception:
                # The mirror is the last gate. When it refuses a document (over the hard
                # cap, or an entry that no longer round-trips) sqlite must go back to
                # exactly the rows it held - not to a re-derived document, whose
                # rendering is lossy for compressed rows.
                try:
                    self._memory_repo.restore_rows(rows_before=rows_before)
                    self._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)
                except Exception as restore_exc:
                    logger.error(f"memory commit rollback failed: {restore_exc}")
                raise
            prior_owner_by_ref: dict[str, str] = {
                str(row.get("note_ref") or "").strip(): str(row.get("memory_id") or "").strip()
                for row in rows_before
                if str(row.get("note_ref") or "").strip()
            }
            surviving_ids = {str(row.get("memory_id") or "").strip() for row in self._memory_repo.list_memories()}

            def _record_reaped(removed_refs: list[str]) -> None:
                for ref in removed_refs:
                    owner = prior_owner_by_ref.get(ref, "")
                    if not owner:
                        reason = "unreferenced"
                    elif owner not in surviving_ids:
                        reason = "memory_deleted"
                    else:
                        reason = "ref_dropped"
                    validated.reaped_notes.append({"note_ref": ref, "reason": reason})

            _record_reaped(self._cleanup_orphan_notes_locked())
            compression_result = self._run_post_commit_compression_locked(now_iso=now_iso)
            final_snapshot = self.snapshot_text()
            _record_reaped(self._cleanup_orphan_notes_locked())
            validated.memory_chars_after = len(final_snapshot)
            validated.document_preview = self._document_preview(final_snapshot)
            validated.compression_triggered = bool(compression_result.get("compression_triggered"))
            validated.compressed_memory_ids = list(compression_result.get("compressed_memory_ids") or [])
            validated.deleted_by_compression_ids = list(compression_result.get("deleted_by_compression_ids") or [])

    @classmethod
    def _processed_batch_retention_delta(cls) -> timedelta:
        return timedelta(days=int(cls._PROCESSED_BATCH_RETENTION_DAYS))

    def _prune_processed_batches(
        self,
        rows: list[dict[str, Any]],
        *,
        now_iso: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        effective_now = self._parse_optional_iso(now_iso) or self._parse_optional_iso(self._now_iso()) or datetime.now().astimezone()
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=datetime.now().astimezone().tzinfo)
        cutoff = effective_now - self._processed_batch_retention_delta()
        kept_rows: list[dict[str, Any]] = []
        changed = False
        for payload in list(rows or []):
            if not isinstance(payload, dict):
                changed = True
                continue
            processed_at = self._parse_optional_iso(payload.get("processed_at"))
            if processed_at is None:
                kept_rows.append(payload)
                continue
            if processed_at.tzinfo is None:
                processed_at = processed_at.replace(tzinfo=effective_now.tzinfo)
            if processed_at < cutoff:
                changed = True
                continue
            kept_rows.append(payload)
        return kept_rows, changed

    def _run_post_commit_compression_locked(self, *, now_iso: str) -> dict[str, Any]:
        return self._compress_memory_snapshot_if_needed(now_iso=now_iso)

    def _compress_memory_snapshot_if_needed(self, *, now_iso: str | None = None) -> dict[str, Any]:
        effective_now = str(now_iso or self._now_iso()).strip()
        trigger = int(getattr(self.config.document, "compress_trigger_chars", 16000) or 16000)
        target = int(getattr(self.config.document, "compress_target_chars", 13000) or 13000)
        compressed_memory_ids: list[str] = []
        deleted_by_compression_ids: list[str] = []
        if len(self.snapshot_text()) <= trigger:
            return {
                "compression_triggered": False,
                "compressed_memory_ids": compressed_memory_ids,
                "deleted_by_compression_ids": deleted_by_compression_ids,
            }

        compression_triggered = False
        while len(self.snapshot_text()) > target:
            mutated_in_pass = False
            for row in self._memory_repo.compression_candidates():
                if len(self.snapshot_text()) <= target:
                    break
                memory_id = str(row.get("memory_id") or "").strip()
                if not memory_id:
                    continue
                mutated = False
                if not bool(row.get("is_compressed")):
                    mutated = self._memory_repo.mark_compressed(memory_id)
                    if mutated:
                        compressed_memory_ids.append(memory_id)
                elif not bool(row.get("from_user")):
                    mutated = self._memory_repo.delete_memory(memory_id)
                    if mutated:
                        deleted_by_compression_ids.append(memory_id)
                if not mutated:
                    continue
                compression_triggered = True
                mutated_in_pass = True
                self._rebuild_memory_snapshot_from_sqlite(now_iso=effective_now)
            if not mutated_in_pass:
                break

        return {
            "compression_triggered": compression_triggered,
            "compressed_memory_ids": compressed_memory_ids,
            "deleted_by_compression_ids": deleted_by_compression_ids,
        }

    def _referenced_note_refs(self) -> list[str]:
        """Note names the store owns. Ownership is a column, not a rendering artefact:
        a compressed row projects to its minimal string and a rewrite can drop the
        marker, so scanning the projection would sweep notes that are still in use."""
        repo = getattr(self, "_memory_repo", None)
        if repo is None:
            return self._collect_note_refs(self.snapshot_text())
        return repo.referenced_note_refs()

    def _cleanup_orphan_notes_locked(self) -> list[str]:
        """Delete note files the store no longer owns; return the removed refs.

        The removal is reported to the caller so it reaches the terminal ledger: an
        unrecoverable file deletion that leaves no trace cannot be triaged later.
        """
        active_refs = set(self._referenced_note_refs())
        removed: list[str] = []
        for path in self.notes_dir.glob("*.md"):
            if path.stem in active_refs:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path.stem)
        return removed

    @staticmethod
    def _collect_note_refs(document_text: str) -> list[str]:
        return sorted({match.group("ref") for match in _NOTE_REF_RE.finditer(str(document_text or ""))})

    def _read_review_state(self) -> dict[str, Any]:
        default_state = {"sessions": {}}
        if not self.review_state_file.exists():
            return default_state
        with self._io_lock:
            try:
                payload = json.loads(self.review_state_file.read_text(encoding="utf-8") or "{}")
            except Exception:
                return default_state
        if not isinstance(payload, dict):
            return default_state
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return default_state
        normalized_sessions: dict[str, Any] = {}
        for raw_session_key, raw_state in sessions.items():
            session_key = str(raw_session_key or "").strip()
            if not session_key or not isinstance(raw_state, dict):
                continue
            pending_turns = [
                dict(item)
                for item in list(raw_state.get("pending_turns") or [])
                if isinstance(item, dict)
            ]
            stage_versions = {
                str(key or "").strip(): str(value or "").strip()
                for key, value in dict(raw_state.get("stage_versions") or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            }
            reported_tool_keys: list[str] = []
            seen_reported_tool_keys: set[str] = set()
            for raw_reported_key in list(raw_state.get("reported_tool_keys") or []):
                normalized_reported_key = str(raw_reported_key or "").strip()
                if not normalized_reported_key or normalized_reported_key in seen_reported_tool_keys:
                    continue
                seen_reported_tool_keys.add(normalized_reported_key)
                reported_tool_keys.append(normalized_reported_key)
            reported_tool_keys = reported_tool_keys[-_REVIEW_REPORTED_TOOL_ID_CAP:]
            normalized_sessions[session_key] = {
                "pending_turns": pending_turns,
                "stage_versions": stage_versions,
                "reported_tool_keys": reported_tool_keys,
            }
        return {"sessions": normalized_sessions}

    def _write_review_state(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload or {})
        sessions = normalized.get("sessions")
        if not isinstance(sessions, dict):
            normalized["sessions"] = {}
        with self._io_lock:
            self._atomic_write_text(
                self.review_state_file,
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            )

    def _review_window_turns(self) -> int:
        return max(int(getattr(self.config.queue, "review_interval_turns", 5) or 5), 1)

    @staticmethod
    def _memory_date_text(now_iso: str | None = None) -> str:
        value = datetime.fromisoformat(str(now_iso or MemoryManager._now_iso()).strip())
        return f"{value.year}/{value.month}/{value.day}"

    def _generate_memory_id(self, existing_ids: set[str]) -> str:
        alphabet = string.ascii_letters + string.digits
        for _ in range(2048):
            candidate = "".join(secrets.choice(alphabet) for _ in range(6))
            if candidate not in existing_ids:
                return candidate
        raise RuntimeError("unable to allocate unique memory id")

    @staticmethod
    def _normalize_memory_content_text(text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            raise _MemoryAgentValidationError("memory content must not be empty")
        if "\n" in normalized or "\r" in normalized:
            raise _MemoryAgentValidationError("memory content must be one line")
        return normalized

    def _normalize_minimal_memory_text(self, text: str, *, summary_max_chars: int) -> str:
        normalized = self._normalize_memory_content_text(text)
        if len(normalized) > max(int(summary_max_chars or 0), 1):
            raise _MemoryAgentValidationError(f"minimal_memory exceeds {summary_max_chars} chars")
        normalized_core = re.sub(r"\s+", " ", _NOTE_REF_RE.sub("", normalized)).strip()
        if normalized_core.count("->") != 1:
            raise _MemoryAgentValidationError("minimal_memory must use 条件->要求关键词")
        condition_text, keyword_text = [part.strip() for part in normalized_core.split("->", 1)]
        if not condition_text or not keyword_text:
            raise _MemoryAgentValidationError("minimal_memory must use 条件->要求关键词")
        return normalized

    @staticmethod
    def _content_exceeds_message(summary_max_chars: int) -> str:
        limit = max(int(summary_max_chars or 0), 1)
        return (
            f"memory content exceeds {limit} chars；"
            f"请压缩为“摘要 + note”：正文只保留一句不超过 {limit} 字的独立规则，细节放入 note"
        )

    def _minimal_compression_fallback_write(
        self,
        *,
        batch: MemoryBatch,
        before_text: str,
        sessions: list[Any],
    ) -> _MemoryValidatedWrite | None:
        for session in sessions:
            candidate = self._minimal_write_from_session(
                batch=batch,
                before_text=before_text,
                session=session,
            )
            if candidate is not None:
                return candidate
        return None

    def _minimal_write_from_session(
        self,
        *,
        batch: MemoryBatch,
        before_text: str,
        session: Any,
    ) -> _MemoryValidatedWrite | None:
        if getattr(session, "apply_batch_count", 0) != 1 or not isinstance(getattr(session, "applied_batch", None), dict):
            return None
        payload = dict(session.applied_batch or {})
        minimal_adds: list[dict[str, str]] = []
        for item in list(payload.get("adds") or []):
            if not isinstance(item, dict):
                continue
            minimal = str(item.get("minimal_memory") or "").strip()
            if not minimal:
                continue
            minimal_adds.append(
                {
                    "content": minimal,
                    "minimal_memory": minimal,
                    "decision_source": str(item.get("decision_source") or "").strip().lower(),
                }
            )
        minimal_rewrites: list[dict[str, str]] = []
        for item in list(payload.get("rewrites") or []):
            if not isinstance(item, dict):
                continue
            memory_id = str(item.get("id") or "").strip()
            minimal = str(item.get("minimal_memory") or "").strip()
            if not memory_id or not minimal:
                continue
            minimal_rewrites.append({"id": memory_id, "content": minimal, "minimal_memory": minimal})
        if not minimal_adds and not minimal_rewrites:
            return None
        fresh = _MemoryToolSession(snapshot_text=before_text, notes_dir=self.notes_dir)
        fresh.apply_batch_count = 1
        fresh.applied_batch = {
            "adds": minimal_adds,
            "rewrites": minimal_rewrites,
            "deletes": list(payload.get("deletes") or []),
            "note_upserts": dict(payload.get("note_upserts") or {}),
            "inspired_memory_ids": list(payload.get("inspired_memory_ids") or []),
            "noop_reason": str(payload.get("noop_reason") or "").strip(),
        }
        try:
            return self._build_validated_write_from_apply_batch(
                before_text=before_text,
                session=fresh,
                batch=batch,
            )
        except _MemoryAgentValidationError:
            return None

    @staticmethod
    def _allowed_review_flush_sources() -> set[str]:
        return {"token_compression", "session_boundary"}

    @staticmethod
    def _compact_review_stage_summary(canonical_summary: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(canonical_summary, dict):
            return None
        compact: dict[str, Any] = {}
        active_stage_id = str(canonical_summary.get("active_stage_id") or "").strip()
        if active_stage_id:
            compact["active_stage_id"] = active_stage_id
        stages: list[dict[str, str]] = []
        for raw_stage in list(canonical_summary.get("stages") or []):
            if not isinstance(raw_stage, dict):
                continue
            entry: dict[str, str] = {}
            for key in ("stage_id", "stage_goal", "completed_stage_summary"):
                value = str(raw_stage.get(key) or "").strip()
                if value:
                    entry[key] = value
            if entry:
                stages.append(entry)
        if stages:
            compact["stages"] = stages
        return compact or None

    @staticmethod
    def _review_stage_identity(stage: dict[str, Any]) -> str:
        stage_id = str(stage.get("stage_id") or "").strip()
        if stage_id:
            return stage_id
        encoded = json.dumps(stage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"anonymous:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _review_stage_version(stage: dict[str, Any]) -> str:
        payload = {
            "stage_goal": str(stage.get("stage_goal") or "").strip(),
            "completed_stage_summary": str(stage.get("completed_stage_summary") or "").strip(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _compact_review_stage_summary_delta(
        *,
        canonical_summary: dict[str, Any] | None,
        stage_versions: dict[str, str] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        compact = MemoryManager._compact_review_stage_summary(canonical_summary)
        normalized_versions = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in dict(stage_versions or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        if not isinstance(compact, dict):
            return None, normalized_versions

        next_versions = dict(normalized_versions)
        delta_stages: list[dict[str, str]] = []
        for stage in list(compact.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            identity = MemoryManager._review_stage_identity(stage)
            version = MemoryManager._review_stage_version(stage)
            if normalized_versions.get(identity) == version:
                continue
            next_versions[identity] = version
            delta_stages.append(stage)

        if not delta_stages:
            return None, next_versions

        delta: dict[str, Any] = {"stages": delta_stages}
        active_stage_id = str(compact.get("active_stage_id") or "").strip()
        if active_stage_id and any(str(stage.get("stage_id") or "").strip() == active_stage_id for stage in delta_stages):
            delta["active_stage_id"] = active_stage_id
        return delta, next_versions

    @staticmethod
    def _message_to_request_dict(message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return dict(message)
        message_type = str(getattr(message, "type", "") or "").strip().lower()
        if message_type == "system":
            role = "system"
        elif message_type == "human":
            role = "user"
        elif message_type == "tool":
            role = "tool"
        else:
            role = "assistant"
        payload = {
            "role": role,
            "content": MemoryManager._response_text(message),
        }
        if role == "tool":
            name = str(getattr(message, "name", "") or "").strip()
            tool_call_id = str(getattr(message, "tool_call_id", "") or "").strip()
            if name:
                payload["name"] = name
            if tool_call_id:
                payload["tool_call_id"] = tool_call_id
        elif role == "assistant":
            tool_calls = MemoryManager._normalize_tool_calls(message)
            if tool_calls:
                payload["tool_calls"] = tool_calls
        return payload

    def _memory_window_guard(
        self,
        *,
        runtime_config: Any,
        model_chain: list[str],
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        usage_total: dict[str, int],
        request_artifacts: list[dict[str, Any]],
    ) -> None:
        """记忆车道不切块：估算请求超过容量就带真实原因失败，不逐轮撞同一个 400。

        容量只取 声明窗口 − 输出余量（与节点/会话车道同一个函数推导），**不**套
        它们那道"实测跨度 × 0.9"夹刀：记忆批次是 append-only 的多轮循环，下一轮必然
        把上一轮的 prompt、模型输出和工具结果一起重发，估算恒大于此前任何一轮的真实
        跨度，夹刀等于让每一批都死在第 2 轮。窗口声明虚高时由 provider 的回包判——
        那条路径仍按 `provider_error` 停车。超限即止，拒发按 `window_refusal` 停车，
        是确定性失败，不参与成功信号自动重排。
        """
        refs = [str(item or "").strip() for item in list(model_chain or []) if str(item or "").strip()]
        if not refs:
            return
        try:
            info = resolve_send_model_context_window_info(config=runtime_config, model_refs=refs)
            window = int(getattr(info, "context_window_tokens", 0) or 0)
        except Exception:
            return
        if window <= 0:
            return
        try:
            estimated = int(estimate_runtime_provider_request_preview_tokens(
                provider_request_body=None,
                request_messages=list(request_messages or []),
                tool_schemas=list(tool_schemas or []),
            ) or 0)
        except Exception:
            return
        capacity, _source = resolve_chunk_capacity(
            context_window_tokens=window,
            envelope_tokens=0,
        )
        if capacity > 0 and estimated <= capacity:
            return
        raise _MemoryProviderError(
            (
                "memory send exceeds model window: "
                f"估算={estimated} 容量={capacity} 窗口={window}"
            ),
            usage=dict(usage_total),
            request_artifacts=list(request_artifacts),
            discard_reason="window_refusal",
        )

    def _memory_request_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        return [self._message_to_request_dict(message) for message in list(messages or [])]

    @staticmethod
    def _extract_provider_request_metadata(response: Any) -> dict[str, Any]:
        metadata = getattr(response, "response_metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = {
            **metadata,
            "provider_request_meta": getattr(response, "provider_request_meta", None)
            or metadata.get("provider_request_meta"),
            "provider_request_body": getattr(response, "provider_request_body", None)
            or metadata.get("provider_request_body"),
        }
        return {
            "provider_request_id": str(
                metadata.get("provider_request_id") or metadata.get("request_id") or ""
            ).strip(),
            "provider_request_meta": metadata.get("provider_request_meta"),
            "provider_request_body": metadata.get("provider_request_body"),
        }

    def _persist_memory_request_artifact(
        self,
        *,
        phase: str,
        batch_op: str,
        queue_request_ids: list[str],
        model_chain: list[str],
        request_messages: list[dict[str, Any]],
        response: Any,
    ) -> dict[str, Any] | None:
        provider_metadata = self._extract_provider_request_metadata(response)
        if not (
            provider_metadata["provider_request_id"]
            or provider_metadata["provider_request_meta"] is not None
            or provider_metadata["provider_request_body"] is not None
        ):
            return None
        artifact_id = self._request_id("memory_request")
        artifact_path = self.request_artifacts_dir / f"{artifact_id}.json"
        payload = {
            "artifact_id": artifact_id,
            "created_at": self._now_iso(),
            "phase": str(phase or "").strip() or "memory",
            "batch_op": str(batch_op or "").strip() or "write",
            "queue_request_ids": [str(item or "").strip() for item in list(queue_request_ids or []) if str(item or "").strip()],
            "model_chain": [str(item or "").strip() for item in list(model_chain or []) if str(item or "").strip()],
            "request_messages": list(request_messages or []),
            "actual_request_diagnostics": build_actual_request_diagnostics(
                request_messages=request_messages,
                tool_schemas=None,
            ),
            "provider_request_id": provider_metadata["provider_request_id"],
            "provider_request_meta": provider_metadata["provider_request_meta"],
            "provider_request_body": provider_metadata["provider_request_body"],
        }
        self._atomic_write_text(artifact_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return {
            "artifact_id": artifact_id,
            "path": str(artifact_path.resolve()),
            "provider_request_id": provider_metadata["provider_request_id"],
        }

    @staticmethod
    def _provider_request_ids_from_artifacts(request_artifacts: list[dict[str, Any]]) -> list[str]:
        provider_request_ids: list[str] = []
        seen: set[str] = set()
        for artifact in list(request_artifacts or []):
            provider_request_id = str((artifact or {}).get("provider_request_id") or "").strip()
            if not provider_request_id or provider_request_id in seen:
                continue
            seen.add(provider_request_id)
            provider_request_ids.append(provider_request_id)
        return provider_request_ids

    @staticmethod
    def _request_artifact_paths(request_artifacts: list[dict[str, Any]]) -> list[str]:
        return [
            str((artifact or {}).get("path") or "").strip()
            for artifact in list(request_artifacts or [])
            if str((artifact or {}).get("path") or "").strip()
        ]

    @staticmethod
    def _precheck_batch_for_processing(batch: MemoryBatch) -> str:
        if not isinstance(batch, MemoryBatch) or not list(batch.items or []):
            return "empty_batch"
        for item in batch.items:
            if not str(item.payload_text or "").strip():
                return "empty_payload_text"
        return ""

    def _append_terminal_history(
        self,
        *,
        batch: MemoryBatch,
        status: str,
        op: str,
        processed_at: str,
        discard_reason: str = "",
        usage: dict[str, int] | None = None,
        model_chain: list[str] | None = None,
        attempt_count: int = 0,
        validated: _MemoryValidatedWrite | None = None,
        provider_request_ids: list[str] | None = None,
        request_artifact_paths: list[str] | None = None,
        error: str = "",
        fallback: str = "",
    ) -> dict[str, Any]:
        payload = {
            "batch_id": self._request_id(batch.op),
            "op": str(op or "").strip() or batch.op,
            "source_op": batch.op,
            "status": str(status or "").strip() or "applied",
            "processed_at": str(processed_at or "").strip() or self._now_iso(),
            "request_ids": [item.request_id for item in batch.items],
            "request_count": len(batch.items),
            "decision_sources": [item.decision_source for item in batch.items],
            "payload_texts": [item.payload_text for item in batch.items],
            "usage": {
                "input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
                "output_tokens": int((usage or {}).get("output_tokens", 0) or 0),
                "cache_read_tokens": int((usage or {}).get("cache_read_tokens", 0) or 0),
            },
            "model_chain": [str(item or "").strip() for item in list(model_chain or []) if str(item or "").strip()],
            "attempt_count": int(attempt_count or 0),
            "provider_request_ids": [str(item or "").strip() for item in list(provider_request_ids or []) if str(item or "").strip()],
            "request_artifact_paths": [str(item or "").strip() for item in list(request_artifact_paths or []) if str(item or "").strip()],
        }
        normalized_discard_reason = str(discard_reason or "").strip()
        if normalized_discard_reason:
            payload["discard_reason"] = normalized_discard_reason
        normalized_error = str(error or "").strip()
        if normalized_error:
            payload["error"] = normalized_error
        normalized_fallback = str(fallback or "").strip()
        if normalized_fallback:
            payload["fallback"] = normalized_fallback
        if validated is not None:
            payload["memory_chars_after"] = len(self.snapshot_text())
            payload["note_refs_written"] = list(validated.note_refs_written)
            payload["document_preview"] = self._document_preview(self.snapshot_text())
            change_preview = self._build_change_preview(validated)
            if change_preview:
                payload["change_preview"] = change_preview
            structured_changes = self._build_structured_changes(validated)
            if structured_changes:
                payload["changes"] = structured_changes
            payload["inspired_memory_ids"] = list(validated.inspired_memory_ids)
            payload["compression_triggered"] = bool(validated.compression_triggered)
            payload["compressed_memory_ids"] = list(validated.compressed_memory_ids)
            payload["deleted_by_compression_ids"] = list(validated.deleted_by_compression_ids)
            normalized_write_mode = str(validated.write_mode or "").strip().lower()
            if normalized_write_mode:
                payload["write_mode"] = normalized_write_mode
            normalized_noop_reason = str(validated.noop_reason or "").strip()
            if normalized_noop_reason:
                payload["noop_reason"] = normalized_noop_reason
            normalized_already_satisfied = str(validated.already_satisfied or "").strip()
            if normalized_already_satisfied:
                payload["already_satisfied"] = normalized_already_satisfied
        self._append_ops_payload(payload)
        return payload

    @staticmethod
    def _review_tool_record_key(*, stage_id: str, round_id: str, tool: dict[str, Any]) -> str:
        tool_call_id = str(tool.get("tool_call_id") or "").strip()
        if tool_call_id:
            return tool_call_id
        fingerprint_source = json.dumps(
            {
                "stage_id": str(stage_id or "").strip(),
                "round_id": str(round_id or "").strip(),
                "tool_name": str(tool.get("tool_name") or "").strip(),
                "arguments_text": str(tool.get("arguments_text") or "").strip(),
                "output_head": str(tool.get("output_text") or "").strip()[:64],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return f"fingerprint:{hashlib.sha256(fingerprint_source.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _collect_visible_tool_records(canonical_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Collect user-visible tool records from canonical stages that still carry rounds."""

        records: list[dict[str, Any]] = []
        if not isinstance(canonical_summary, dict):
            return records
        for stage in list(canonical_summary.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id") or "").strip()
            rounds = [item for item in list(stage.get("rounds") or []) if isinstance(item, dict)]
            if not rounds:
                continue
            for round_item in rounds:
                round_id = str(round_item.get("round_id") or "").strip()
                round_text = str(round_item.get("text") or "").strip()
                for tool in list(round_item.get("tools") or []):
                    if not isinstance(tool, dict):
                        continue
                    tool_name = str(tool.get("tool_name") or "").strip()
                    if not tool_name:
                        continue
                    status = str(tool.get("status") or "").strip().lower()
                    records.append(
                        {
                            "key": MemoryManager._review_tool_record_key(
                                stage_id=stage_id,
                                round_id=round_id,
                                tool=tool,
                            ),
                            "stage_id": stage_id,
                            "round_id": round_id,
                            "round_text": round_text,
                            "tool_name": tool_name,
                            "status": status or "unknown",
                            "is_error": bool(status) and status != "success",
                            "arguments_text": str(tool.get("arguments_text") or "").strip(),
                            "output_preview_text": str(tool.get("output_preview_text") or "").strip(),
                            "output_text": str(tool.get("output_text") or "").strip(),
                        }
                    )
        return records

    @staticmethod
    def _merge_review_stage_payload(
        *,
        canonical_summary: dict[str, Any] | None,
        stage_delta: dict[str, Any] | None,
        tool_records: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Nest new tool records under their stages, mirroring the on-screen order."""

        records_by_stage: dict[str, list[dict[str, Any]]] = {}
        rounds_with_text: set[str] = set()
        for record in list(tool_records or []):
            entry: dict[str, Any] = {
                "tool_name": str(record.get("tool_name") or "").strip() or "tool",
                "status": str(record.get("status") or "").strip() or "unknown",
            }
            arguments_text = str(record.get("arguments_text") or "").strip()
            if arguments_text:
                # 入参长度由写入侧统一限（canonical_context 的 4000 字），这里不再二次截断。
                entry["args"] = arguments_text
            preview_text = MemoryManager._inline_preview_text(
                record.get("output_preview_text"),
                max_chars=_REVIEW_TOOL_PREVIEW_MAX_CHARS,
            )
            if preview_text:
                entry["output_preview"] = preview_text
            output_text = str(record.get("output_text") or "").strip()
            if output_text:
                entry["output_full"] = MemoryManager._inline_preview_text(
                    output_text,
                    max_chars=_REVIEW_TOOL_OUTPUT_MAX_CHARS,
                )
            round_key = str(record.get("round_id") or "").strip()
            round_text = str(record.get("round_text") or "").strip()
            # Round narration is user-visible as-is and is intentionally not truncated.
            if round_text and (not round_key or round_key not in rounds_with_text):
                if round_key:
                    rounds_with_text.add(round_key)
                entry["中途输出"] = round_text
            stage_id = str(record.get("stage_id") or "").strip() or "(unknown-stage)"
            records_by_stage.setdefault(stage_id, []).append(entry)

        delta_stages: dict[str, dict[str, Any]] = {}
        for raw_stage in list((stage_delta or {}).get("stages") or []):
            if not isinstance(raw_stage, dict):
                continue
            stage_id = str(raw_stage.get("stage_id") or "").strip()
            if stage_id:
                delta_stages[stage_id] = raw_stage

        merged: list[dict[str, Any]] = []
        emitted_stage_ids: set[str] = set()

        canonical_stages = (
            list(canonical_summary.get("stages") or [])
            if isinstance(canonical_summary, dict)
            else []
        )
        canonical_stage_by_id: dict[str, dict[str, Any]] = {}
        for raw_stage in canonical_stages:
            if isinstance(raw_stage, dict):
                canonical_stage_id = str(raw_stage.get("stage_id") or "").strip()
                if canonical_stage_id:
                    canonical_stage_by_id.setdefault(canonical_stage_id, raw_stage)

        def _build_stage_entry(stage_id: str) -> dict[str, Any] | None:
            delta_entry = delta_stages.get(stage_id)
            stage_records = records_by_stage.pop(stage_id, None)
            if delta_entry is None and not stage_records:
                return None
            # 阶段条目必须自描述：目标/总结优先取本轮 delta；阶段只携带新工具记录时
            # 回退可见面当前值，保证复核载荷里的阶段始终是完整未压缩形态。
            source_entry = delta_entry or canonical_stage_by_id.get(stage_id) or {}
            stage_entry: dict[str, Any] = {"stage_id": stage_id}
            stage_goal = str(source_entry.get("stage_goal") or "").strip()
            if stage_goal:
                stage_entry["stage_goal"] = stage_goal
            if stage_records:
                stage_entry["tool_records"] = stage_records
            completed_summary = str(source_entry.get("completed_stage_summary") or "").strip()
            if completed_summary:
                stage_entry["completed_stage_summary"] = completed_summary
            return stage_entry

        for raw_stage in canonical_stages:
            if not isinstance(raw_stage, dict):
                continue
            stage_id = str(raw_stage.get("stage_id") or "").strip()
            if not stage_id or stage_id in emitted_stage_ids:
                continue
            stage_entry = _build_stage_entry(stage_id)
            if stage_entry is None:
                continue
            emitted_stage_ids.add(stage_id)
            merged.append(stage_entry)
        for stage_id in list(records_by_stage.keys()) + [
            stage_id for stage_id in delta_stages if stage_id not in emitted_stage_ids
        ]:
            if stage_id in emitted_stage_ids:
                continue
            stage_entry = _build_stage_entry(stage_id)
            if stage_entry is None:
                continue
            emitted_stage_ids.add(stage_id)
            merged.append(stage_entry)

        active_stage_id = str((stage_delta or {}).get("active_stage_id") or "").strip()
        if not merged and not active_stage_id:
            return None
        payload: dict[str, Any] = {}
        if active_stage_id:
            payload["active_stage_id"] = active_stage_id
        if merged:
            payload["stages"] = merged
        return payload or None

    @staticmethod
    def _review_window_payload(*, session_key: str, pending_turns: list[dict[str, Any]]) -> str:
        sections = [f"[session_key] {str(session_key or '').strip()}"]
        for index, item in enumerate(list(pending_turns or []), start=1):
            payload_text = str(item.get("payload_text") or "").strip()
            if not payload_text:
                continue
            sections.append(f"## 窗口回合 {index}")
            sections.append(payload_text)
        return "\n\n".join(sections).strip()

    @staticmethod
    def _review_turn_payload(
        *,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
        compression_summary: dict[str, Any] | None,
        canonical_summary: dict[str, Any] | None,
        stage_delta: dict[str, Any] | None = None,
        tool_records: list[dict[str, Any]] | None = None,
    ) -> str:
        _ = compression_summary
        parts = [f"[turn_id] {str(turn_id or '').strip() or '(unknown)'}"]
        normalized_user_messages = [
            str(item or "").strip()
            for item in list(user_messages or [])
            if str(item or "").strip()
        ]
        if normalized_user_messages:
            parts.append("user_messages:")
            parts.extend(f"- {item}" for item in normalized_user_messages)
        stage_payload = MemoryManager._merge_review_stage_payload(
            canonical_summary=canonical_summary,
            stage_delta=stage_delta,
            tool_records=tool_records,
        )
        if isinstance(stage_payload, dict) and stage_payload:
            parts.append("stages:")
            parts.append(json.dumps(stage_payload, ensure_ascii=False, indent=2))
        assistant = str(assistant_text or "").strip()
        if assistant:
            parts.append("assistant_text:")
            parts.append(assistant)
        return "\n".join(str(item) for item in parts if str(item).strip()).strip()

    @staticmethod
    def _window_turn_sections(section: str, *, max_tokens: int) -> list[str]:
        """单个窗口回合超容量时按 stage 边界切；stage 是不可分原子组，永不腰斩。

        回合头（turn_id、user_messages）和回合尾（assistant_text）每片都完整带上：
        拆的是"带多少个阶段"，不是"少带这一轮说了什么"。
        """

        value = str(section or "")
        marker = "\nstages:\n"
        head_end = value.find(marker)
        if head_end < 0:
            return [value]
        head = value[:head_end]
        body_start = head_end + len(marker)
        try:
            data, consumed = json.JSONDecoder().raw_decode(value[body_start:])
        except ValueError:
            return [value]
        tail = value[body_start + consumed:]
        stages = data.get("stages") if isinstance(data, dict) else None
        if not isinstance(stages, list) or len(stages) < 2:
            return [value]
        active_stage_id = data.get("active_stage_id") if isinstance(data, dict) else None

        def _render(group: list[Any]) -> str:
            payload: dict[str, Any] = {"stages": group}
            if active_stage_id:
                payload["active_stage_id"] = active_stage_id
            return f"{head}{marker}{json.dumps(payload, ensure_ascii=False, indent=2)}{tail}"

        # 每个阶段只数一次、按累计和装箱。此前每收一个阶段就把已积累的整组重新
        # 序列化再逐字符数一遍，597 个阶段的实盘载荷因此要 26.7 秒（同一份载荷按
        # 一次性数是 0.4 秒），而重试点下去的那次同步请求就是这么把前端 30 秒超时擦过去的。
        stage_sizes = [max(1, estimate_tokens(json.dumps(stage, ensure_ascii=False, indent=2))) for stage in stages]
        frame_tokens = estimate_tokens(f"{head}{marker}{tail}")
        budget = max(1, max_tokens - frame_tokens)
        if sum(stage_sizes) <= budget:
            return [value]
        sections: list[str] = []
        group: list[Any] = []
        used_tokens = 0
        for stage, size in zip(stages, stage_sizes):
            if group and used_tokens + size > budget:
                sections.append(_render(group))
                group = [stage]
                used_tokens = size
            else:
                group.append(stage)
                used_tokens += size
        if group:
            sections.append(_render(group))
        return sections or [value]

    @classmethod
    def _split_window_payload(cls, payload_text: str, *, max_tokens: int) -> list[str]:
        """复核窗口的拆片：先按窗口回合装箱，单回合仍超则按 stage 边界再切。

        目标不是少带内容（工具明细按既有合同继续全带），而是让每一片都落在发送闸门
        之内——超容量的整批只会停在停车区等人工，那条路上没有自动出口。
        单片仍超（一个 stage 就装不下）时照原样发出，由闸门按真实原因拒。
        """

        value = str(payload_text or "").strip()
        if not value or max_tokens <= 0 or estimate_tokens(value) <= max_tokens:
            return [value] if value else []
        header, _, rest = value.partition("\n\n")
        if not header.startswith("[session_key]"):
            return [value]
        turns = [part.strip() for part in re.split(r"\n\n(?=## 窗口回合 )", rest) if part.strip()]
        if not turns:
            return [value]
        header_tokens = estimate_tokens(header)
        pieces: list[str] = []
        bucket: list[str] = []
        used_tokens = 0

        def flush() -> None:
            nonlocal used_tokens
            if not bucket:
                return
            pieces.append("\n\n".join([header, *bucket]).strip())
            bucket.clear()
            used_tokens = 0

        def emit(section: str) -> None:
            nonlocal used_tokens
            section_tokens = estimate_tokens(section)
            if bucket and used_tokens + section_tokens > max(1, max_tokens - header_tokens):
                flush()
            bucket.append(section)
            used_tokens += section_tokens

        for section in turns:
            if header_tokens + estimate_tokens(section) > max_tokens:
                flush()
                for sub in cls._window_turn_sections(section, max_tokens=max(1, max_tokens - header_tokens)):
                    emit(sub)
                continue
            emit(section)
        flush()
        return pieces or [value]

    def _memory_window_split_target_tokens(self) -> int:
        """拆片目标 = 记忆链上**最小**声明窗口 × `_MEMORY_WINDOW_SPLIT_RATIO`。

        发送闸门按"这一发落到的那颗模型"的窗口算容量，而批次真正发出时落点可能已经
        前进过链位，所以这里取整条链最小的一半：任何一次链前进都还在容量内，
        而不是赌它一定落在链首。拿不到配置就返回 0，表示不拆（闸门那条路照常）。
        """

        try:
            runtime_config, _revision, _changed = get_runtime_config()
        except Exception:
            return 0
        windows: list[int] = []
        for ref in list(self._memory_model_chain(runtime_config) or []):
            try:
                info = resolve_send_model_context_window_info(config=runtime_config, model_refs=[ref])
            except Exception:
                continue
            value = int(getattr(info, "context_window_tokens", 0) or 0)
            if value > 0:
                windows.append(value)
        if not windows:
            return 0
        return int(min(windows) * _MEMORY_WINDOW_SPLIT_RATIO)

    async def _enqueue_review_window_requests(
        self,
        *,
        session_key: str,
        payload_text: str,
        decision_source: str,
        trigger_source: str,
    ) -> list[MemoryQueueRequest]:
        """复核窗口入队的唯一出口：超容量就拆成多条 pending 行，逐条消化。"""

        max_tokens = self._memory_window_split_target_tokens()
        requests: list[MemoryQueueRequest] = []
        blocked_rules: list[str] = []
        for piece in self._split_window_payload(payload_text, max_tokens=max_tokens):
            if not str(piece or "").strip():
                continue
            request = MemoryQueueRequest(
                op="write",
                decision_source=decision_source,
                payload_text=piece,
                created_at=self._now_iso(),
                request_id=self._request_id("write"),
                session_key=str(session_key or "").strip(),
                trigger_source=trigger_source,
            )
            blocked, rule = scan_memory_candidate(piece)
            if blocked:
                # No model is waiting on this lane, so the piece is dropped here and
                # only the ledger keeps the record.
                self._reject_blocked_candidate(request, rule)
                blocked_rules.append(rule)
                continue
            requests.append(request)
        for request in requests:
            await self._append_queue_request(request)
        if len(requests) > 1:
            logger.info(
                "复核窗口超单片容量，按 {} 片分别入队：session={} 目标={} tok",
                len(requests),
                str(session_key or "").strip(),
                max_tokens,
            )
        if blocked_rules:
            logger.warning(
                "复核窗口有 {} 片被内容闸门拦下（rules={}）：session={}",
                len(blocked_rules),
                ",".join(sorted(set(blocked_rules))),
                str(session_key or "").strip(),
            )
        return requests

    async def record_turn_for_review(
        self,
        *,
        session_key: str,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
        compression_summary: dict[str, Any] | None,
        canonical_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_session = str(session_key or "").strip()
        if not normalized_session:
            return {"ok": True, "status": "ignored", "reason": "missing_session_key"}
        state = self._read_review_state()
        sessions = dict(state.get("sessions") or {})
        session_state = dict(sessions.get(normalized_session) or {})
        stage_versions = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in dict(session_state.get("stage_versions") or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        reported_tool_keys = [
            str(key or "").strip()
            for key in list(session_state.get("reported_tool_keys") or [])
            if str(key or "").strip()
        ]
        reported_tool_key_set = set(reported_tool_keys)
        collected_tool_records = self._collect_visible_tool_records(canonical_summary)
        new_tool_records = [
            record for record in collected_tool_records if record["key"] not in reported_tool_key_set
        ]
        # 记录时全量捕获：本轮新出现的工具记录不设每轮数量上限，全部固化进缓冲载荷，
        # 保证复核窗口内的阶段始终以未压缩形态送达，不受会话侧阶段压缩影响。
        next_reported_tool_keys = (
            reported_tool_keys + [str(record.get("key") or "").strip() for record in new_tool_records]
        )[-_REVIEW_REPORTED_TOOL_ID_CAP:]
        stage_summary_delta, next_stage_versions = self._compact_review_stage_summary_delta(
            canonical_summary=canonical_summary,
            stage_versions=stage_versions,
        )
        pending_turns = [
            dict(item)
            for item in list(session_state.get("pending_turns") or [])
            if isinstance(item, dict)
        ]
        payload_text = self._review_turn_payload(
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
            compression_summary=compression_summary,
            canonical_summary=canonical_summary,
            stage_delta=stage_summary_delta,
            tool_records=new_tool_records,
        )
        if not payload_text:
            return {"ok": True, "status": "ignored", "reason": "empty_turn"}
        pending_turns.append(
            {
                "turn_id": str(turn_id or "").strip(),
                "payload_text": payload_text,
                "created_at": self._now_iso(),
            }
        )
        if len(pending_turns) < self._review_window_turns():
            sessions[normalized_session] = {
                "pending_turns": pending_turns,
                "stage_versions": next_stage_versions,
                "reported_tool_keys": next_reported_tool_keys,
            }
            self._write_review_state({"sessions": sessions})
            return {
                "ok": True,
                "status": "buffered",
                "pending_turn_count": len(pending_turns),
            }
        payload = self._review_window_payload(session_key=normalized_session, pending_turns=pending_turns)
        sessions[normalized_session] = {
            "pending_turns": [],
            "stage_versions": next_stage_versions,
            "reported_tool_keys": next_reported_tool_keys,
        }
        self._write_review_state({"sessions": sessions})
        requests = await self._enqueue_review_window_requests(
            session_key=normalized_session,
            payload_text=payload,
            decision_source="self",
            trigger_source="ordinary_turn_window",
        )
        return {
            "ok": True,
            "status": "queued",
            "request_id": str(requests[0].request_id if requests else ""),
            "request_ids": [request.request_id for request in requests],
            "split_parts": len(requests),
            "pending_turn_count": 0,
        }

    async def flush_review_window(
        self,
        *,
        session_key: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        normalized_session = str(session_key or "").strip()
        if not normalized_session:
            return {"ok": True, "status": "ignored", "reason": "missing_session_key"}
        normalized_trigger = str(trigger_source or "").strip() or "token_compression"
        if normalized_trigger not in self._allowed_review_flush_sources():
            return {"ok": True, "status": "ignored", "reason": "unsupported_trigger_source"}
        state = self._read_review_state()
        sessions = dict(state.get("sessions") or {})
        session_state = dict(sessions.get(normalized_session) or {})
        pending_turns = [
            dict(item)
            for item in list(session_state.get("pending_turns") or [])
            if isinstance(item, dict)
        ]
        if not pending_turns:
            return {"ok": True, "status": "idle", "reason": "no_pending_turns"}
        payload = self._review_window_payload(session_key=normalized_session, pending_turns=pending_turns)
        sessions[normalized_session] = {
            "pending_turns": [],
            "stage_versions": {
                str(key or "").strip(): str(value or "").strip()
                for key, value in dict(session_state.get("stage_versions") or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            },
            "reported_tool_keys": [
                str(key or "").strip()
                for key in list(session_state.get("reported_tool_keys") or [])
                if str(key or "").strip()
            ][-_REVIEW_REPORTED_TOOL_ID_CAP:],
        }
        self._write_review_state({"sessions": sessions})
        requests = await self._enqueue_review_window_requests(
            session_key=normalized_session,
            payload_text=payload,
            decision_source="self",
            trigger_source=normalized_trigger,
        )
        return {
            "ok": True,
            "status": "queued",
            "request_id": str(requests[0].request_id if requests else ""),
            "request_ids": [request.request_id for request in requests],
            "split_parts": len(requests),
            "pending_turn_count": 0,
        }

    def clear_review_window(self, *, session_key: str) -> None:
        normalized_session = str(session_key or "").strip()
        if not normalized_session:
            return
        state = self._read_review_state()
        sessions = dict(state.get("sessions") or {})
        if normalized_session not in sessions:
            return
        sessions.pop(normalized_session, None)
        self._write_review_state({"sessions": sessions})

    def _reject_blocked_candidate(self, request: MemoryQueueRequest, rule: str) -> dict[str, Any]:
        """Keep a blocked candidate out of the queue, but not out of the ledger.

        An enqueue-side refusal has no batch behind it, so the terminal row is written
        from the single request: without it, "how much did the guard swallow, and what"
        is unanswerable and a rule that misfires looks like silent memory amnesia.
        """
        processed_at = self._now_iso()
        self._append_terminal_history(
            batch=MemoryBatch(op=request.op, items=[request]),
            status="discarded",
            op=request.op,
            processed_at=processed_at,
            discard_reason="content_guard_blocked",
            error=f"content guard blocked this candidate (rule: {rule}); it was never queued",
        )
        return {
            "ok": False,
            "status": "blocked",
            "error": "content_guard_blocked",
            "rule": rule,
            "request_id": request.request_id,
        }

    async def enqueue_write_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        normalized_payload = str(payload_text or "").strip()
        if not normalized_payload:
            return {"ok": True, "status": "ignored", "reason": "empty_payload"}
        request = MemoryQueueRequest(
            op="write",
            decision_source=self._normalize_decision_source(decision_source),
            payload_text=normalized_payload,
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("write"),
        )
        blocked, rule = scan_memory_candidate(request.payload_text)
        if blocked:
            return self._reject_blocked_candidate(request, rule)
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_delete_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        normalized_payload = str(payload_text or "").strip()
        if not normalized_payload:
            return {"ok": True, "status": "ignored", "reason": "empty_payload"}
        request = MemoryQueueRequest(
            op="delete",
            decision_source=self._normalize_decision_source(decision_source),
            payload_text=normalized_payload,
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("delete"),
        )
        blocked, rule = scan_memory_candidate(request.payload_text)
        if blocked:
            return self._reject_blocked_candidate(request, rule)
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        user_messages: list[str],
        assistant_text: str,
        turn_id: str,
        compression_summary: dict[str, Any] | None = None,
        canonical_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = channel, chat_id
        return await self.record_turn_for_review(
            session_key=session_key,
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
            compression_summary=compression_summary,
            canonical_summary=canonical_summary,
        )

    async def enqueue_pre_compression_flush(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        context_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        _ = channel, chat_id, context_messages
        return await self.flush_review_window(
            session_key=session_key,
            trigger_source="pre_compression_flush",
        )

    async def enqueue_session_boundary_flush(
        self,
        *,
        session_key: str,
        channel: str = "",
        chat_id: str = "",
        trigger_source: str = "session_boundary",
    ) -> dict[str, Any]:
        _ = channel, chat_id, trigger_source
        result = await self.flush_review_window(
            session_key=session_key,
            trigger_source="session_boundary",
        )
        self.clear_review_window(session_key=session_key)
        return result

    def _memory_agent_tools(self, session: _MemoryToolSession) -> list[_MemoryTool]:
        def memory_read_note(ref: str) -> str:
            """Read one existing note by ref without the .md suffix."""

            return session.read_note(ref)

        def memory_apply_batch(
            adds: list[dict[str, Any]] | None = None,
            rewrites: list[dict[str, Any]] | None = None,
            deletes: list[str] | None = None,
            note_upserts: dict[str, str] | None = None,
            inspired_memory_ids: list[str] | None = None,
            noop_reason: str | None = None,
            already_satisfied: str | None = None,
        ) -> dict[str, Any]:
            """Stage one complete memory mutation batch."""

            return session.apply_batch(
                adds=adds,
                rewrites=rewrites,
                deletes=deletes,
                note_upserts=note_upserts,
                inspired_memory_ids=inspired_memory_ids,
                noop_reason=noop_reason,
                already_satisfied=already_satisfied,
            )

        return [
            _build_memory_tool(name="memory_read_note", fn=memory_read_note),
            _build_memory_tool(name="memory_apply_batch", fn=memory_apply_batch),
        ]

    def _memory_agent_user_prompt(
        self,
        *,
        batch: MemoryBatch,
        snapshot_text: str,
        repair_reason: str,
    ) -> str:
        note_index = sorted(path.stem for path in self.notes_dir.glob("*.md"))
        requests = [
            {
                "request_id": item.request_id,
                "op": item.op,
                "decision_source": item.decision_source,
                "payload_text": item.payload_text,
                "created_at": item.created_at,
                "trigger_source": item.trigger_source,
                "session_key": item.session_key,
            }
            for item in batch.items
        ]
        repair_block = f"\n上一轮输出无效：{repair_reason}\n请修复后重试。\n" if repair_reason else ""
        return (
            f"当前记忆正文：\n{snapshot_text or '(empty)'}\n\n"
            f"现有 note refs：{json.dumps(note_index, ensure_ascii=False)}\n"
            f"当前批次类型：{batch.op}\n"
            f"待处理请求：\n{json.dumps(requests, ensure_ascii=False, indent=2)}\n"
            "提交要求：\n"
            "- 只允许使用一次 memory_apply_batch 完成所有修改。\n"
            "- adds 条目必须包含 content、minimal_memory 和 decision_source。\n"
            "- content 写法按来源分：decision_source=user 写成陈述句（用户偏好什么、某对象是什么），"
            "不要写成给自己的命令；decision_source=self 写成条件+要求（当……时，要……）。\n"
            "- rewrites 条目必须包含 id、content 和 minimal_memory；系统会保留原 source 并刷新日期。\n"
            "- delete 请求要解析成精确的记忆 id，并把这些 id 放进 deletes。\n"
            "- delete 批次没有零变更出口：目标内容已不在当前正文里（已被改写或被更早的批次删掉）时，"
            "单独提交 already_satisfied 说明已满足，不要提交 noop_reason。\n"
            "- note_upserts 只写需要新增或改写的 note。\n"
            "- inspired_memory_ids 可选，用于列出当前完整 snapshot 里对本批判断有实质帮助的 memory id。\n"
            "- minimal_memory 格式应写成 `条件->要求关键词`，必要时优先附加 `见noteid:<id>`，并兼容 `ref:<id>`。\n"
            "- 当本轮无需任何记忆或 note 变更时，可单独提交 noop_reason（仅 write 批次）。\n"
            f"{repair_block}"
        )

    @staticmethod
    async def _execute_memory_tool(tools: list[Any], tool_call: dict[str, Any]) -> dict[str, Any]:
        name = str(tool_call.get("name") or "").strip()
        args = dict(tool_call.get("args") or {})
        tool_map = {str(getattr(item, "name", "") or ""): item for item in list(tools or [])}
        target = tool_map.get(name)
        if target is None:
            return {"ok": False, "tool": name, "error": f"unknown memory tool: {name}"}
        try:
            result = target.invoke(args)
            if asyncio.iscoroutine(result):
                result = await result
            return {"ok": True, "tool": name, "result": result}
        except Exception as exc:
            return {"ok": False, "tool": name, "error": str(exc or "memory tool failed").strip()}

    def _build_validated_write_from_apply_batch(
        self,
        *,
        before_text: str,
        session: _MemoryToolSession,
        batch: MemoryBatch,
    ) -> _MemoryValidatedWrite:
        if session.apply_batch_count != 1 or not isinstance(session.applied_batch, dict):
            raise _MemoryAgentValidationError("memory agent must call memory_apply_batch exactly once")
        payload = dict(session.applied_batch or {})
        adds = list(payload.get("adds") or [])
        rewrites = list(payload.get("rewrites") or [])
        deletes = list(payload.get("deletes") or [])
        note_upserts = dict(payload.get("note_upserts") or {})
        raw_inspired_memory_ids = payload.get("inspired_memory_ids")
        if raw_inspired_memory_ids is None:
            inspired_memory_ids: list[str] = []
        elif not isinstance(raw_inspired_memory_ids, list):
            raise _MemoryAgentValidationError("inspired_memory_ids must be a list")
        else:
            inspired_memory_ids = list(raw_inspired_memory_ids)
        noop_reason = str(payload.get("noop_reason") or "").strip()
        already_satisfied = str(payload.get("already_satisfied") or "").strip()

        if batch.op == "delete" and (adds or rewrites):
            raise _MemoryAgentValidationError("delete batch may not add or rewrite memories")
        if batch.op == "delete" and noop_reason:
            raise _MemoryAgentValidationError(_DELETE_NOOP_RULE)
        if batch.op != "delete" and already_satisfied:
            raise _MemoryAgentValidationError("already_satisfied is only valid for delete batches")
        if noop_reason and (adds or rewrites or deletes or note_upserts):
            raise _MemoryAgentValidationError("noop_reason may not be combined with add, rewrite, delete, or note_upsert")
        if already_satisfied and (adds or rewrites or deletes or note_upserts or noop_reason):
            raise _MemoryAgentValidationError(
                "already_satisfied may not be combined with add, rewrite, delete, note_upsert, or noop_reason"
            )

        before_entries = parse_memory_document(before_text)
        existing_by_id = {entry.memory_id: entry for entry in before_entries}
        if len(existing_by_id) != len(before_entries):
            raise _MemoryAgentValidationError("current memory snapshot contains duplicate ids")

        delete_ids = {str(item or "").strip() for item in deletes if str(item or "").strip()}
        missing_delete_ids = sorted(memory_id for memory_id in delete_ids if memory_id not in existing_by_id)
        if missing_delete_ids:
            raise _MemoryAgentValidationError(f"delete ids not found: {', '.join(missing_delete_ids)}")

        rewrite_by_id: dict[str, str] = {}
        rewrite_minimal_by_id: dict[str, str] = {}
        rewrite_note_ref_by_id: dict[str, str] = {}
        effective_rewrites: list[dict[str, str]] = []
        minimal_note_refs: set[str] = set()
        summary_limit = int(getattr(self.config.document, "summary_max_chars", 300) or 300)
        for item in rewrites:
            if not isinstance(item, dict):
                raise _MemoryAgentValidationError("rewrite item must be an object")
            memory_id = str(item.get("id") or "").strip()
            content = self._normalize_memory_content_text(str(item.get("content") or ""))
            minimal_memory = self._normalize_minimal_memory_text(
                str(item.get("minimal_memory") or ""),
                summary_max_chars=summary_limit,
            )
            if memory_id not in existing_by_id:
                raise _MemoryAgentValidationError(f"rewrite id not found: {memory_id}")
            rewrite_by_id[memory_id] = content
            rewrite_minimal_by_id[memory_id] = minimal_memory
            rewrite_note_ref = self._extract_note_ref(minimal_memory)
            if rewrite_note_ref:
                rewrite_note_ref_by_id[memory_id] = rewrite_note_ref
                minimal_note_refs.add(rewrite_note_ref)

        snapshot_memory_ids = {
            str(entry.memory_id or "").strip()
            for entry in before_entries
            if str(entry.memory_id or "").strip()
        }
        missing_inspired_ids = sorted(memory_id for memory_id in inspired_memory_ids if memory_id not in snapshot_memory_ids)
        if missing_inspired_ids:
            raise _MemoryAgentValidationError(f"inspired ids not found: {', '.join(missing_inspired_ids)}")

        existing_ids = set(existing_by_id)
        date_text = self._memory_date_text()
        after_entries: list[MemoryEntry] = []
        after_entry_ids: set[str] = set()
        seen_summary_keys: set[tuple[str, str]] = set()
        for entry in before_entries:
            if entry.memory_id in delete_ids:
                continue
            if entry.memory_id in rewrite_by_id:
                updated_note_ref = rewrite_note_ref_by_id.get(entry.memory_id, "") or self._extract_note_ref(rewrite_by_id[entry.memory_id])
                updated_summary = self._summary_with_note_ref(rewrite_by_id[entry.memory_id], updated_note_ref)
                if len(updated_summary) > summary_limit:
                    raise _MemoryAgentValidationError(self._content_exceeds_message(summary_limit))
                updated = MemoryEntry(
                    memory_id=entry.memory_id,
                    date_text=date_text,
                    source=entry.source,
                    summary=updated_summary,
                    note_ref=updated_note_ref,
                )
                summary_key = (updated.source, updated.summary)
                if summary_key in seen_summary_keys:
                    continue
                seen_summary_keys.add(summary_key)
                after_entries.append(updated)
                after_entry_ids.add(updated.memory_id)
                effective_rewrites.append(
                    {
                        "id": entry.memory_id,
                        "content": rewrite_by_id[entry.memory_id],
                        "minimal_memory": rewrite_minimal_by_id[entry.memory_id],
                    }
                )
                continue
            summary_key = (entry.source, entry.summary)
            if summary_key in seen_summary_keys:
                continue
            seen_summary_keys.add(summary_key)
            after_entries.append(entry)
            after_entry_ids.add(entry.memory_id)

        effective_adds: list[dict[str, str]] = []
        for item in adds:
            if not isinstance(item, dict):
                raise _MemoryAgentValidationError("add item must be an object")
            content = self._normalize_memory_content_text(str(item.get("content") or ""))
            minimal_memory = self._normalize_minimal_memory_text(
                str(item.get("minimal_memory") or ""),
                summary_max_chars=summary_limit,
            )
            decision_source = str(item.get("decision_source") or "").strip().lower()
            if decision_source not in {"user", "self"}:
                raise _MemoryAgentValidationError("add decision_source must be user or self")
            add_note_ref = self._extract_note_ref(minimal_memory)
            if add_note_ref:
                minimal_note_refs.add(add_note_ref)
            summary = self._summary_with_note_ref(content, add_note_ref)
            if len(summary) > summary_limit:
                raise _MemoryAgentValidationError(self._content_exceeds_message(summary_limit))
            summary_key = (decision_source, summary)
            if summary_key in seen_summary_keys:
                continue
            seen_summary_keys.add(summary_key)
            memory_id = self._generate_memory_id(existing_ids)
            existing_ids.add(memory_id)
            after_entry_ids.add(memory_id)
            after_entries.append(
                MemoryEntry(
                    memory_id=memory_id,
                    date_text=date_text,
                    source=decision_source,
                    summary=summary,
                    note_ref=(self._extract_note_ref(summary)),
                )
            )
            effective_adds.append(
                {
                    "id": memory_id,
                    "content": content,
                    "minimal_memory": minimal_memory,
                    "decision_source": decision_source,
                }
            )

        document_text = "".join(format_memory_entry(entry) for entry in after_entries)
        try:
            validate_memory_document(
                document_text,
                summary_max_chars=summary_limit,
                document_max_chars=int(getattr(self.config.document, "document_max_chars", 20000) or 20000),
            )
        except ValueError as exc:
            raise _MemoryAgentValidationError(str(exc or "memory document invalid").strip()) from exc
        existing_refs = {path.stem for path in self.notes_dir.glob("*.md")}
        normalized_note_upserts = {
            str(ref): self._normalize_note_text(content)
            for ref, content in note_upserts.items()
            if str(ref or "").strip()
        }
        note_refs = {
            str(entry.note_ref or "").strip()
            for entry in after_entries
            if str(entry.note_ref or "").strip()
        }
        note_refs.update(minimal_note_refs)
        missing_refs = sorted(ref for ref in note_refs if ref not in existing_refs and ref not in normalized_note_upserts)
        if missing_refs:
            raise _MemoryAgentValidationError(f"missing note refs: {', '.join(missing_refs)}")
        effective_delete_ids = sorted(
            {
                str(entry.memory_id or "").strip()
                for entry in before_entries
                if str(entry.memory_id or "").strip() and str(entry.memory_id or "").strip() not in after_entry_ids
            }
        )
        return _MemoryValidatedWrite(
            document_text=self._normalize_document_text(document_text),
            note_writes=normalized_note_upserts,
            note_refs_written=sorted(normalized_note_upserts.keys()),
            memory_chars_after=len(self._normalize_document_text(document_text)),
            document_preview=self._document_preview(document_text),
            adds=effective_adds,
            rewrites=effective_rewrites,
            deletes=effective_delete_ids,
            note_upserts=normalized_note_upserts,
            inspired_memory_ids=list(inspired_memory_ids),
            write_mode=self._validated_write_mode(
                adds=adds,
                rewrites=rewrites,
                deletes=deletes,
                note_upserts=note_upserts,
                noop_reason=noop_reason,
                already_satisfied=already_satisfied,
            ),
            noop_reason=noop_reason,
            already_satisfied=already_satisfied,
        )

    @staticmethod
    def _validated_write_mode(
        *,
        adds: list[Any],
        rewrites: list[Any],
        deletes: list[Any],
        note_upserts: dict[str, Any],
        noop_reason: str,
        already_satisfied: str = "",
    ) -> str:
        if str(noop_reason or "").strip() or str(already_satisfied or "").strip():
            return ""
        has_adds = bool(list(adds or []))
        has_rewrites_or_deletes = bool(list(rewrites or []) or list(deletes or []))
        has_note_only_mutation = bool(dict(note_upserts or {})) and not has_adds and not has_rewrites_or_deletes
        has_existing_mutations = has_rewrites_or_deletes or has_note_only_mutation
        if has_adds and has_existing_mutations:
            return "mixed"
        if has_adds:
            return "add"
        if has_existing_mutations:
            return "rewrite"
        return ""

    @staticmethod
    def _extract_note_ref(text: str) -> str:
        match = _NOTE_REF_RE.search(str(text or "").strip())
        return str(match.group("ref") or "").strip() if match else ""

    @staticmethod
    def _summary_with_note_ref(summary: str, note_ref: str) -> str:
        normalized_ref = str(note_ref or "").strip()
        normalized_summary = str(summary or "").strip()
        if normalized_ref:
            normalized_summary = re.sub(r"\s+", " ", _NOTE_REF_RE.sub("", normalized_summary)).strip()
            return f"{normalized_summary} ref:{normalized_ref}".strip()
        normalized_summary = _NOTE_REF_RE.sub(
            lambda match: f"ref:{str(match.group('ref') or '').strip()}",
            normalized_summary,
        )
        return normalized_summary

    def _validate_candidate_state(
        self,
        *,
        batch: MemoryBatch,
        before_text: str,
        session: _MemoryToolSession,
    ) -> _MemoryValidatedWrite:
        return self._build_validated_write_from_apply_batch(
            before_text=before_text,
            session=session,
            batch=batch,
        )

    async def _run_memory_agent_batch(
        self,
        *,
        batch: MemoryBatch,
        runtime_config: Any,
        runtime_revision: int,
        model_chain: list[str],
    ) -> dict[str, Any]:
        before_text = self.snapshot_text()
        total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        request_artifacts: list[dict[str, Any]] = []
        queue_request_ids = [item.request_id for item in batch.items if str(item.request_id or "").strip()]

        current_runtime_config = runtime_config
        current_runtime_revision = int(runtime_revision or 0)
        current_model_chain = list(model_chain or [])
        processing_batch = batch
        if str(batch.op or "").strip().lower() == "assess":
            processing_batch = MemoryBatch(
                op="write",
                items=[
                    MemoryQueueRequest(
                        op="write",
                        decision_source=item.decision_source,
                        payload_text=item.payload_text,
                        created_at=item.created_at,
                        request_id=item.request_id,
                        trigger_source=item.trigger_source,
                        session_key=item.session_key,
                        status=item.status,
                        processing_started_at=item.processing_started_at,
                        last_error_text=item.last_error_text,
                        last_error_at=item.last_error_at,
                        retry_after=item.retry_after,
                    )
                    for item in batch.items
                ],
            )

        agent_cfg = getattr(self.config, "agent", None)
        repair_attempt_limit = int(getattr(agent_cfg, "repair_attempt_limit", 2) if agent_cfg is not None else 2)
        total_attempts = 1 + max(repair_attempt_limit, 0)
        last_error = "memory agent did not stage a batch"
        attempt_sessions: list[Any] = []
        for attempt_index in range(total_attempts):
            if attempt_index > 0:
                current_runtime_config, current_runtime_revision, current_model_chain = self._reload_runtime_config_for_memory_retry(
                    runtime_config=current_runtime_config,
                    runtime_revision=current_runtime_revision,
                    model_chain=current_model_chain,
                )
            repair_reason = last_error if attempt_index > 0 else ""
            try:
                attempt = await self._run_memory_agent_attempt(
                    batch=processing_batch,
                    runtime_config=current_runtime_config,
                    before_text=before_text,
                    repair_reason=repair_reason,
                    queue_request_ids=queue_request_ids,
                    model_chain=current_model_chain,
                )
            except _MemoryProviderError as exc:
                # Provider 层失败不做批内修复重试（几秒内的连续尝试只会撞同一个
                # 限流/故障窗口），直接带真实错误返回，由上层停车等待成功信号或人工重试。
                # 本地发送前预检拒发（window_refusal）同样不做批内重试：载荷不变则
                # 重算必然得到同一个估算，而尺子读的是这一发落到的那颗模型（记忆车道
                # 没有组预测，取链首那颗的声明窗口）。
                self._merge_usage(total_usage, dict(exc.usage or {}))
                request_artifacts.extend(list(exc.request_artifacts or []))
                return {
                    "validated": None,
                    "usage": total_usage,
                    "attempt_count": attempt_index + 1,
                    "assessed_text": None,
                    "discard_reason": exc.discard_reason,
                    "error": exc.error_text,
                    "model_chain": list(current_model_chain),
                    "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
                    "request_artifact_paths": self._request_artifact_paths(request_artifacts),
                }
            self._merge_usage(total_usage, attempt.usage)
            request_artifacts.extend(list(attempt.request_artifacts or []))
            attempt_sessions.append(attempt.session)
            try:
                validated = self._validate_candidate_state(
                    batch=processing_batch,
                    before_text=before_text,
                    session=attempt.session,
                )
                return {
                    "validated": validated,
                    "usage": total_usage,
                    "attempt_count": attempt_index + 1,
                    "assessed_text": None,
                    "model_chain": list(current_model_chain),
                    "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
                    "request_artifact_paths": self._request_artifact_paths(request_artifacts),
                }
            except _MemoryAgentValidationError as exc:
                last_error = str(exc or "memory agent output invalid").strip()
                continue
        fallback_validated = self._minimal_compression_fallback_write(
            batch=processing_batch,
            before_text=before_text,
            sessions=attempt_sessions,
        )
        if fallback_validated is not None:
            return {
                "validated": fallback_validated,
                "usage": total_usage,
                "attempt_count": total_attempts,
                "assessed_text": None,
                "model_chain": list(current_model_chain),
                "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
                "request_artifact_paths": self._request_artifact_paths(request_artifacts),
                "fallback": "minimal_compression",
            }
        return {
            "validated": None,
            "usage": total_usage,
            "attempt_count": total_attempts,
            "assessed_text": None,
            "discard_reason": "rejected",
            "error": last_error,
            "model_chain": list(current_model_chain),
            "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
            "request_artifact_paths": self._request_artifact_paths(request_artifacts),
        }

    async def run_due_batch_once(self, *, now_iso: str | None = None) -> dict[str, Any]:
        worker_lease = self._try_acquire_memory_worker_lease()
        if worker_lease is None:
            return {"ok": True, "status": "worker_lease_unavailable", "processed": 0}

        try:
            processed_request_ids = self._processed_request_ids()
            dropped_request_ids = self._drop_processed_queue_requests(
                processed_request_ids=processed_request_ids,
            )
            effective_now = str(now_iso or self._now_iso()).strip()
            batch = await self.collect_due_batch(now_iso=effective_now)
            if batch is None or not batch.items:
                if dropped_request_ids:
                    return {
                        "ok": True,
                        "status": "already_processed",
                        "processed": 0,
                        "request_ids": list(dropped_request_ids),
                    }
                return {"ok": True, "status": "idle", "processed": 0}
            duplicate_request_ids = [
                item.request_id
                for item in batch.items
                if str(item.request_id or "").strip() in processed_request_ids
            ]
            if duplicate_request_ids:
                self._drop_request_ids(set(duplicate_request_ids))
                return {
                    "ok": True,
                    "status": "already_processed",
                    "processed": 0,
                    "request_ids": list(duplicate_request_ids),
                }
            precheck_error = self._precheck_batch_for_processing(batch)
            if precheck_error:
                self._drop_request_ids({item.request_id for item in batch.items})
                processed_at = self._now_iso()
                self._append_terminal_history(
                    batch=batch,
                    status="discarded",
                    op=batch.op,
                    processed_at=processed_at,
                    discard_reason="precheck_failed",
                    error=precheck_error,
                )
                return {
                    "ok": True,
                    "status": "discarded",
                    "discard_reason": "precheck_failed",
                    "op": batch.op,
                    "processed": len(batch.items),
                    "request_ids": [item.request_id for item in batch.items],
                    "processed_at": processed_at,
                }
            if str(batch.items[0].status or "pending").strip() != "processing":
                self._mark_batch_processing(batch.items, effective_now)

            try:
                runtime_config, _revision, _changed = get_runtime_config()
            except Exception as exc:
                return self._mark_batch_error(batch, effective_now, str(exc or "memory runtime config unavailable"))

            model_chain = self._memory_model_chain(runtime_config)
            if not model_chain:
                return self._mark_batch_blocked(batch, effective_now, "memory role not configured")

            try:
                result = await self._run_memory_agent_batch(
                    batch=batch,
                    runtime_config=runtime_config,
                    runtime_revision=int(_revision or 0),
                    model_chain=model_chain,
                )
            except Exception as exc:
                # 运行时异常（含 provider 抛出的链路耗尽错误）按瞬时失败停车：
                # 离开主队列、保留完整载荷与错误，等成功信号或人工重试，不再队头无限自旋。
                logger.warning("memory batch failed, parking for retry: {}", exc)
                failed_record = self._park_failed_batch(
                    batch,
                    category="provider_error",
                    discard_reason="agent_exception",
                    error=str(exc or "memory batch failed").strip(),
                    usage=None,
                    model_chain=model_chain,
                    attempt_count=0,
                    trigger="exception",
                    now_iso=effective_now,
                )
                return self._parked_batch_report(batch, failed_record, discard_reason="agent_exception")

            discard_reason = str(result.get("discard_reason") or "").strip()
            effective_model_chain = list(result.get("model_chain") or model_chain)
            if discard_reason:
                # 处理失败不再写 durable discarded 终态并删行（数据永久丢失），
                # 而是移入失败停车区 memory/failed.jsonl：
                # - provider_error（限流/超时等瞬时错误）→ 队列下次成功应用时自动重入队尾
                # - window_refusal（本地发送前预检拒发）→ 确定性失败，仅人工重试
                # - rejected（模型真实响应但违反协议）→ 仅人工确认重试
                # 终态记录推迟到操作员显式放弃（discard）时才写入 ops.jsonl。
                if discard_reason == "provider_error":
                    category = "provider_error"
                elif discard_reason == "window_refusal":
                    category = "window_refusal"
                else:
                    category = "protocol"
                failed_record = self._park_failed_batch(
                    batch,
                    category=category,
                    discard_reason=discard_reason,
                    error=str(result.get("error") or "").strip(),
                    usage=result.get("usage"),
                    model_chain=effective_model_chain,
                    attempt_count=int(result.get("attempt_count", 0) or 0),
                    provider_request_ids=list(result.get("provider_request_ids") or []),
                    request_artifact_paths=list(result.get("request_artifact_paths") or []),
                    trigger="initial",
                    now_iso=effective_now,
                )
                return self._parked_batch_report(
                    batch,
                    failed_record,
                    discard_reason=discard_reason,
                    usage=result.get("usage"),
                    attempt_count=int(result.get("attempt_count", 0) or 0),
                    model_chain=effective_model_chain,
                    provider_request_ids=list(result.get("provider_request_ids") or []),
                    request_artifact_paths=list(result.get("request_artifact_paths") or []),
                )

            self._drop_request_ids({item.request_id for item in batch.items})
            self._commit_validated_write(result["validated"])
            # 成功信号：清掉本批 request_id 对应的停车记录（终于成功了），
            # 并把停车区里最早的一条 provider/瞬时错误批次重新入队尾（无成功信号则永不自动重试）。
            self._purge_failed_records_for_request_ids({item.request_id for item in batch.items})
            requeued_failed = self._requeue_next_parked_on_success(now_iso=effective_now)
            processed_at = self._now_iso()
            fallback = str(result.get("fallback") or "").strip()
            processed_payload = self._append_terminal_history(
                batch=batch,
                status="applied",
                op="write" if batch.op == "assess" else batch.op,
                processed_at=processed_at,
                usage=result.get("usage"),
                model_chain=effective_model_chain,
                attempt_count=int(result["attempt_count"]),
                validated=result["validated"],
                provider_request_ids=list(result.get("provider_request_ids") or []),
                request_artifact_paths=list(result.get("request_artifact_paths") or []),
                fallback=fallback,
            )
            return {
                "ok": True,
                "status": "applied",
                "op": batch.op,
                "processed": len(batch.items),
                "request_ids": [item.request_id for item in batch.items],
                "attempt_count": int(result["attempt_count"]),
                "usage": dict(processed_payload["usage"]),
                "model_chain": list(effective_model_chain),
                "provider_request_ids": list(processed_payload["provider_request_ids"]),
                "request_artifact_paths": list(processed_payload["request_artifact_paths"]),
                "processed_at": processed_at,
                **({"noop_reason": str(processed_payload.get("noop_reason") or "").strip()} if str(processed_payload.get("noop_reason") or "").strip() else {}),
                **(
                    {"already_satisfied": str(processed_payload.get("already_satisfied") or "").strip()}
                    if str(processed_payload.get("already_satisfied") or "").strip()
                    else {}
                ),
                **({"fallback": fallback} if fallback else {}),
                **({"requeued_failed_id": str(requeued_failed.get("failed_id") or "")} if requeued_failed else {}),
            }
        finally:
            _release_file_lock(worker_lease)

    # ------------------------------------------------------------------
    # 失败停车区（memory/failed.jsonl）
    #
    # 处理失败的批次不再写 durable discarded 终态并删除队列行，而是整体移入
    # 停车区文件：主队列继续流动，失败批次保留完整载荷与错误历史。
    # - category=provider_error：限流/超时/上游错误等瞬时失败。队列每次成功
    #   应用新批次时（成功信号），自动把停车区里最早的一条重新入队尾；没有
    #   成功信号则一直停车，不产生任何自动重试成本，也不设次数上限。
    # - category=window_refusal：本地发送前预检拒发（载荷估算超过这一发落点模型的
    #   容量＝其声明窗口 − 输出余量）。
    #   载荷不变则重算必然同一个数，属确定性失败，不参与成功信号自动重排，只接受
    #   人工重试（换模型窗口声明或批内载荷变小之后才有意义）。
    # - category=protocol：模型真实响应但违反 memory_apply_batch 协议（批内
    #   已做过修复重试），只接受人工确认重试。
    # 只有操作员显式放弃（discard）时才写入 ops.jsonl 终态记录，request_id
    # 由此进入已处理集合完成幂等闭环。
    # ------------------------------------------------------------------

    def _read_failed_records(self) -> list[dict[str, Any]]:
        if not self.failed_file.exists():
            return []
        with self._io_lock:
            lines = self.failed_file.read_text(encoding="utf-8").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines:
            if not str(line or "").strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    def _write_failed_records(self, rows: list[dict[str, Any]]) -> None:
        with self._io_lock:
            text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
            self.failed_file.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_text(self.failed_file, (f"{text}\n" if text else ""))

    def _failed_queue_row_from_payload(self, payload: Any) -> MemoryQueueRequest | None:
        try:
            data = dict(payload or {})
            field_names = set(MemoryQueueRequest.__dataclass_fields__)
            row = MemoryQueueRequest(**{key: value for key, value in data.items() if key in field_names})
        except Exception:
            return None
        row = self._normalize_queue_request(row)
        row.status = "pending"
        row.processing_started_at = ""
        row.retry_after = ""
        return row

    @staticmethod
    def _normalized_usage_dict(usage: dict[str, int] | None) -> dict[str, int]:
        return {
            "input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
            "output_tokens": int((usage or {}).get("output_tokens", 0) or 0),
            "cache_read_tokens": int((usage or {}).get("cache_read_tokens", 0) or 0),
        }

    def _park_failed_batch(
        self,
        batch: MemoryBatch,
        *,
        category: str,
        discard_reason: str,
        error: str,
        usage: dict[str, int] | None = None,
        model_chain: list[str] | None = None,
        attempt_count: int = 0,
        provider_request_ids: list[str] | None = None,
        request_artifact_paths: list[str] | None = None,
        trigger: str = "initial",
        now_iso: str | None = None,
    ) -> dict[str, Any]:
        now = str(now_iso or "").strip() or self._now_iso()
        request_ids = [str(item.request_id or "").strip() for item in batch.items]
        id_set = {rid for rid in request_ids if rid}
        rows = self._read_failed_records()
        existing: dict[str, Any] | None = None
        for row in rows:
            row_ids = {str(rid or "").strip() for rid in list(row.get("request_ids") or [])}
            if row_ids & id_set:
                existing = row
                break
        history_entry = {
            "at": now,
            "category": str(category or "").strip(),
            "discard_reason": str(discard_reason or "").strip(),
            "error": str(error or "").strip(),
            "trigger": str(trigger or "initial").strip(),
        }
        merged_usage = self._normalized_usage_dict(usage)
        if existing is not None:
            existing_usage = self._normalized_usage_dict(existing.get("usage_total"))
            self._merge_usage(existing_usage, merged_usage)
            history = list(existing.get("error_history") or [])
            history.append(history_entry)
            existing.update(
                {
                    "op": batch.op,
                    "request_ids": request_ids,
                    "items": [asdict(item) for item in batch.items],
                    "category": str(category or "").strip(),
                    "discard_reason": str(discard_reason or "").strip(),
                    "status": "parked",
                    "parked_at": now,
                    "requeued_at": "",
                    "park_count": int(existing.get("park_count", 0) or 0) + 1,
                    "llm_attempt_count": int(existing.get("llm_attempt_count", 0) or 0) + max(int(attempt_count or 0), 0),
                    "last_error_text": history_entry["error"],
                    "last_error_at": now,
                    "error_history": history[-50:],
                    "usage_total": existing_usage,
                }
            )
            if model_chain:
                existing["model_chain"] = [str(item or "").strip() for item in list(model_chain) if str(item or "").strip()]
            for key, values in (
                ("provider_request_ids", provider_request_ids),
                ("request_artifact_paths", request_artifact_paths),
            ):
                if not values:
                    continue
                merged = list(existing.get(key) or [])
                for value in values:
                    normalized_value = str(value or "").strip()
                    if normalized_value and normalized_value not in merged:
                        merged.append(normalized_value)
                existing[key] = merged
            record = existing
        else:
            created_ats = [str(item.created_at or "").strip() for item in batch.items if str(item.created_at or "").strip()]
            record = {
                "failed_id": self._request_id("failed"),
                "op": batch.op,
                "request_ids": request_ids,
                "items": [asdict(item) for item in batch.items],
                "category": str(category or "").strip(),
                "discard_reason": str(discard_reason or "").strip(),
                "status": "parked",
                "created_at": min(created_ats) if created_ats else now,
                "first_parked_at": now,
                "parked_at": now,
                "requeued_at": "",
                "park_count": 1,
                "llm_attempt_count": max(int(attempt_count or 0), 0),
                "auto_requeue_count": 0,
                "manual_retry_count": 0,
                "last_error_text": history_entry["error"],
                "last_error_at": now,
                "error_history": [history_entry],
                "usage_total": merged_usage,
                "model_chain": [str(item or "").strip() for item in list(model_chain or []) if str(item or "").strip()],
                "provider_request_ids": [str(item or "").strip() for item in list(provider_request_ids or []) if str(item or "").strip()],
                "request_artifact_paths": [str(item or "").strip() for item in list(request_artifact_paths or []) if str(item or "").strip()],
            }
            rows.append(record)
        self._write_failed_records(rows)
        self._drop_request_ids(id_set)
        _emit_audit_memory_parked(record, trigger=str(trigger or "initial").strip())
        return record

    def _parked_batch_report(
        self,
        batch: MemoryBatch,
        record: dict[str, Any],
        *,
        discard_reason: str = "",
        usage: dict[str, int] | None = None,
        attempt_count: int = 0,
        model_chain: list[str] | None = None,
        provider_request_ids: list[str] | None = None,
        request_artifact_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "parked",
            "discard_reason": str(discard_reason or record.get("discard_reason") or "").strip(),
            "category": str(record.get("category") or "").strip(),
            "failed_id": str(record.get("failed_id") or "").strip(),
            "op": batch.op,
            "processed": 0,
            "request_ids": [item.request_id for item in batch.items],
            "attempt_count": int(attempt_count or 0),
            "usage": self._normalized_usage_dict(usage if usage is not None else record.get("usage_total")),
            "model_chain": list(model_chain if model_chain is not None else (record.get("model_chain") or [])),
            "provider_request_ids": list(provider_request_ids if provider_request_ids is not None else (record.get("provider_request_ids") or [])),
            "request_artifact_paths": list(request_artifact_paths if request_artifact_paths is not None else (record.get("request_artifact_paths") or [])),
            "parked_at": str(record.get("parked_at") or "").strip(),
        }

    def _purge_failed_records_for_request_ids(self, request_ids: set[str]) -> None:
        id_set = {str(rid or "").strip() for rid in request_ids if str(rid or "").strip()}
        if not id_set:
            return
        rows = self._read_failed_records()
        remaining: list[dict[str, Any]] = []
        changed = False
        for row in rows:
            row_ids = {str(rid or "").strip() for rid in list(row.get("request_ids") or [])}
            if row_ids & id_set:
                changed = True
                continue
            remaining.append(row)
        if changed:
            self._write_failed_records(remaining)

    def _requeue_failed_record(
        self,
        rows: list[dict[str, Any]],
        record: dict[str, Any],
        *,
        trigger: str,
        now_iso: str | None = None,
    ) -> dict[str, Any]:
        now = str(now_iso or "").strip() or self._now_iso()
        queue_rows = self._read_queue_requests()
        existing_ids = {str(row.request_id or "").strip() for row in queue_rows}
        requeued_ids: list[str] = []
        split_target = self._memory_window_split_target_tokens()
        for item_payload in list(record.get("items") or []):
            row = self._failed_queue_row_from_payload(item_payload)
            if row is None:
                continue
            if row.request_id and row.request_id in existing_ids:
                continue
            # 重排走与入队同一个拆片器：停车区里存的仍是整份载荷，原样回队只会在
            # 闸门上再撞同一次拒发（实盘那条 1.28MB 记录就是这么停了四轮）。
            for index, piece in enumerate(self._split_window_payload(str(row.payload_text or ""), max_tokens=split_target)):
                piece_row = (
                    row
                    if index == 0
                    else replace(row, request_id=self._request_id(str(row.op or "write")))
                )
                piece_row.payload_text = str(piece or "")
                if piece_row.request_id and piece_row.request_id in existing_ids:
                    continue
                queue_rows.append(piece_row)
                if piece_row.request_id:
                    existing_ids.add(piece_row.request_id)
                requeued_ids.append(piece_row.request_id)
        self._write_queue_requests(queue_rows)
        history = list(record.get("error_history") or [])
        history.append({"at": now, "event": "requeued", "trigger": str(trigger or "auto").strip()})
        record["error_history"] = history[-50:]
        record["status"] = "requeued"
        record["requeued_at"] = now
        if str(trigger or "").strip() == "manual":
            record["manual_retry_count"] = int(record.get("manual_retry_count", 0) or 0) + 1
        else:
            record["auto_requeue_count"] = int(record.get("auto_requeue_count", 0) or 0) + 1
        self._write_failed_records(rows)
        return {
            "failed_id": str(record.get("failed_id") or "").strip(),
            "request_ids": requeued_ids,
            "trigger": str(trigger or "auto").strip(),
            "requeued_at": now,
        }

    def _requeue_next_parked_on_success(self, *, now_iso: str | None = None) -> dict[str, Any] | None:
        """成功信号驱动的自动重试：每次成功应用批次后，把停车区里最早的一条
        provider/瞬时错误批次重新入队尾。没有成功信号就永远不会自动重试。"""
        if not bool(getattr(self.config.queue, "auto_requeue_on_success", True)):
            return None
        rows = self._read_failed_records()
        candidates = [
            row
            for row in rows
            if str(row.get("status") or "").strip() == "parked"
            and str(row.get("category") or "").strip() == "provider_error"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda row: (str(row.get("first_parked_at") or ""), str(row.get("created_at") or "")))
        return self._requeue_failed_record(rows, candidates[0], trigger="auto", now_iso=now_iso)

    @staticmethod
    def _window_payload_chars(text: str, *, start_char: int, limit_chars: int) -> dict[str, Any]:
        """按字符切一段载荷：`start_char`/`end_char` 是 1-based 闭区间，与 content 车道 char 模式同词汇。"""

        value = str(text or "")
        total = len(value)
        start = max(1, min(int(start_char or 1), max(total, 1)))
        body = value[start - 1 : start - 1 + max(1, int(limit_chars or 1))]
        end = start + len(body) - 1 if body else start - 1
        return {
            "text": body,
            "start_char": start,
            "end_char": end,
            "char_count": total,
            "truncated": end < total,
        }

    def _project_payload_for_read(self, text: str) -> dict[str, Any]:
        """单份载荷的首屏投影：给投影后的正文与原文长度，是否截断由两者相减判出。"""

        window = self._window_payload_chars(
            str(text or ""),
            start_char=1,
            limit_chars=_MEMORY_PAYLOAD_PREVIEW_CHARS,
        )
        return {"text": window["text"], "char_count": window["char_count"]}

    def _project_failed_record_for_read(self, record: dict[str, Any]) -> dict[str, Any]:
        """读侧投影：`items[].payload_text` 只留首屏，整份载荷仍留在停车区文件里供回队。"""

        projected_items: list[dict[str, Any]] = []
        for payload in list(record.get("items") or []):
            item = dict(payload) if isinstance(payload, dict) else {}
            window = self._project_payload_for_read(str(item.get("payload_text") or ""))
            item["payload_text"] = window["text"]
            item["payload_char_count"] = window["char_count"]
            projected_items.append(item)
        projected = dict(record)
        projected["items"] = projected_items
        return projected

    def _project_processed_row_for_read(self, row: dict[str, Any]) -> dict[str, Any]:
        """终态行的 `payload_texts` 逐条投首屏，另给一条等长的 `payload_char_counts`。

        两个数组按同一下标对齐，截断与否由 `len(text) < char_counts[i]` 判出，
        不再放第三个平行标志位（三份同源读数会各说各话）。
        """

        projected = dict(row)
        windows = [self._project_payload_for_read(str(text or "")) for text in list(row.get("payload_texts") or [])]
        projected["payload_texts"] = [window["text"] for window in windows]
        projected["payload_char_counts"] = [window["char_count"] for window in windows]
        return projected

    def _project_queue_row_for_read(self, row: dict[str, Any]) -> dict[str, Any]:
        projected = dict(row)
        window = self._project_payload_for_read(str(row.get("payload_text") or ""))
        projected["payload_text"] = window["text"]
        projected["payload_char_count"] = window["char_count"]
        return projected

    def _locate_memory_payload_text(self, kind: str, key: str, request_id: str) -> tuple[str, str]:
        """按 (kind, key[, request_id]) 取回**原文**载荷：三类读面的分页共用这一处定位。"""

        if kind == "failed":
            record = next(
                (row for row in self._read_failed_records() if str(row.get("failed_id") or "").strip() == key),
                None,
            )
            if record is None:
                raise KeyError(f"failed memory record not found: {key}")
            items = [item for item in list(record.get("items") or []) if isinstance(item, dict)]
        elif kind == "processed":
            row = next(
                (item for item in self._read_processed_batches() if str(item.get("batch_id") or "").strip() == key),
                None,
            )
            if row is None:
                raise KeyError(f"processed memory batch not found: {key}")
            texts = [str(text or "") for text in list(row.get("payload_texts") or [])]
            ids = [str(rid or "").strip() for rid in list(row.get("request_ids") or [])]
            index = 0
            if request_id:
                if request_id not in ids:
                    raise KeyError(f"processed memory payload not found: {key}/{request_id}")
                index = ids.index(request_id)
            if index >= len(texts):
                raise KeyError(f"processed memory payload not found: {key}/{request_id or 'first item'}")
            return ids[index] if index < len(ids) else request_id, texts[index]
        else:  # queue
            row = next(
                (item for item in self._read_queue_requests() if str(item.request_id or "").strip() == key),
                None,
            )
            if row is None:
                raise KeyError(f"memory queue request not found: {key}")
            return key, str(row.payload_text or "")

        item = None
        if request_id:
            item = next(
                (row for row in items if str(row.get("request_id") or "").strip() == request_id),
                None,
            )
        else:
            item = items[0] if items else None
        if item is None:
            raise KeyError(f"memory payload not found: {key}/{request_id or 'first item'}")
        return str(item.get("request_id") or "").strip(), str(item.get("payload_text") or "")

    async def get_payload_window(
        self,
        kind: str,
        key: str,
        *,
        request_id: str = "",
        start_char: int = 1,
    ) -> dict[str, Any]:
        """取全文一屏：三类记忆读面共用一个分页实现，按 kind/key 定位原文再切窗。"""

        normalized_kind = str(kind or "").strip().lower()
        normalized_key = str(key or "").strip()
        if normalized_kind not in ("failed", "processed", "queue") or not normalized_key:
            raise KeyError(f"unsupported memory payload source: {normalized_kind or '(empty kind)'}/{normalized_key}")
        located_request_id, text = self._locate_memory_payload_text(
            normalized_kind, normalized_key, str(request_id or "").strip()
        )
        window = self._window_payload_chars(
            text,
            start_char=int(start_char or 1),
            limit_chars=_MEMORY_PAYLOAD_WINDOW_CHARS,
        )
        return {
            "kind": normalized_kind,
            "key": normalized_key,
            "request_id": located_request_id,
            **window,
        }

    async def list_failed_page(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = self._read_failed_records()
        parked = [row for row in rows if str(row.get("status") or "").strip() == "parked"]
        parked.sort(key=lambda row: str(row.get("parked_at") or ""), reverse=True)
        start = max(int(offset or 0), 0)
        page_size = max(int(limit or 0), 0)
        end = start + page_size
        return {
            # 只对这一页做投影：整份载荷可能上百 KB 一条，全量复制会随文件线性变贵。
            "items": [self._project_failed_record_for_read(row) for row in parked[start:end]],
            "total": len(parked),
            "has_more": end < len(parked),
        }

    async def retry_failed_record(self, failed_id: str, *, reason: str = "manual") -> dict[str, Any]:
        target = str(failed_id or "").strip()
        rows = self._read_failed_records()
        record = next((row for row in rows if str(row.get("failed_id") or "").strip() == target), None)
        if record is None:
            raise KeyError(f"failed memory record not found: {target}")
        if str(record.get("status") or "").strip() != "parked":
            raise ValueError(f"failed memory record is not parked (status={record.get('status')})")
        summary = self._requeue_failed_record(rows, record, trigger="manual")
        summary["reason"] = str(reason or "manual").strip()
        return summary

    async def discard_failed_record(self, failed_id: str, *, reason: str = "manual") -> dict[str, Any]:
        target = str(failed_id or "").strip()
        rows = self._read_failed_records()
        record = next((row for row in rows if str(row.get("failed_id") or "").strip() == target), None)
        if record is None:
            raise KeyError(f"failed memory record not found: {target}")
        items: list[MemoryQueueRequest] = []
        for item_payload in list(record.get("items") or []):
            row = self._failed_queue_row_from_payload(item_payload)
            if row is not None:
                items.append(row)
        if not items:
            items = [
                MemoryQueueRequest(
                    op=str(record.get("op") or "write"),
                    decision_source="self",
                    payload_text="",
                    created_at=str(record.get("created_at") or ""),
                    request_id=str(rid or ""),
                )
                for rid in list(record.get("request_ids") or [])
            ]
        batch = MemoryBatch(op=str(record.get("op") or "write"), items=items)
        terminal = self._append_terminal_history(
            batch=batch,
            status="discarded",
            op=str(record.get("op") or "write"),
            processed_at=self._now_iso(),
            discard_reason="operator_discarded",
            usage=record.get("usage_total"),
            model_chain=record.get("model_chain"),
            attempt_count=int(record.get("llm_attempt_count", 0) or 0),
            provider_request_ids=record.get("provider_request_ids"),
            request_artifact_paths=record.get("request_artifact_paths"),
            error=str(record.get("last_error_text") or "").strip(),
        )
        # 记录可能处于 requeued 状态（行已回到队列），放弃时把队列行一并清掉；
        # 终态记录已写入 ops.jsonl，request_id 进入已处理集合，幂等去重兜底。
        self._drop_request_ids({str(rid or "").strip() for rid in list(record.get("request_ids") or []) if str(rid or "").strip()})
        self._write_failed_records(
            [row for row in self._read_failed_records() if str(row.get("failed_id") or "").strip() != target]
        )
        return {
            "failed_id": target,
            "status": "discarded",
            "batch_id": str(terminal.get("batch_id") or "").strip(),
            "request_ids": list(record.get("request_ids") or []),
            "reason": str(reason or "manual").strip(),
            "error_history_count": len(list(record.get("error_history") or [])),
        }

    def _reload_runtime_config_for_memory_retry(
        self,
        *,
        runtime_config: Any,
        runtime_revision: int,
        model_chain: list[str],
    ) -> tuple[Any, int, list[str]]:
        try:
            refreshed_config, refreshed_revision, _changed = get_runtime_config(force=False)
        except Exception:
            return runtime_config, int(runtime_revision or 0), list(model_chain or [])
        normalized_revision = int(refreshed_revision or 0)
        if normalized_revision == int(runtime_revision or 0):
            return runtime_config, int(runtime_revision or 0), list(model_chain or [])
        refreshed_chain = self._memory_model_chain(refreshed_config)
        return refreshed_config, normalized_revision, list(refreshed_chain)
